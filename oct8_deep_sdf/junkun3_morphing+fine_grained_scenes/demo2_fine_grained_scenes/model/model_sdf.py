import torch.nn as nn
import torch
import copy
from tqdm import tqdm
from utils import utils_deepsdf
import numpy as np
"""
Model based on the paper 'DeepSDF'. 
"""
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

from torch.nn.parameter import Parameter, UninitializedParameter
from torch import norm_except_dim
from typing import Any, TypeVar
from torch.nn.modules import Module

__all__ = ['WeightNorm', 'weight_norm', 'remove_weight_norm']

class WeightNorm:
    name: str
    dim: int

    def __init__(self, name: str, dim: int) -> None:
        if dim is None:
            dim = -1
        self.name = name
        self.dim = dim

    # TODO Make return type more specific
    def compute_weight(self, module: Module) -> Any:
        g = getattr(module, self.name + '_g')
        v = getattr(module, self.name + '_v')
        # return _weight_norm(v, g, self.dim)
        return v * (g / torch.norm_except_dim(v, dim=self.dim))

    @staticmethod
    def apply(module, name: str, dim: int) -> 'WeightNorm':
        for hook in module._forward_pre_hooks.values():
            if isinstance(hook, WeightNorm) and hook.name == name:
                raise RuntimeError(f"Cannot register two weight_norm hooks on the same parameter {name}")

        if dim is None:
            dim = -1

        fn = WeightNorm(name, dim)

        weight = getattr(module, name)
        if isinstance(weight, UninitializedParameter):
            raise ValueError(
                'The module passed to `WeightNorm` can\'t have uninitialized parameters. '
                'Make sure to run the dummy forward before applying weight normalization')
        # remove w from parameter list
        del module._parameters[name]

        # add g and v as new parameters and express w as g/||v|| * v
        module.register_parameter(name + '_g', Parameter(norm_except_dim(weight, 2, dim).data))
        module.register_parameter(name + '_v', Parameter(weight.data))
        setattr(module, name, fn.compute_weight(module))

        # recompute weight before every forward()
        module.register_forward_pre_hook(fn)

        return fn

    def remove(self, module: Module) -> None:
        weight = self.compute_weight(module)
        delattr(module, self.name)
        del module._parameters[self.name + '_g']
        del module._parameters[self.name + '_v']
        setattr(module, self.name, Parameter(weight.data))

    def __call__(self, module: Module, inputs: Any) -> None:
        setattr(module, self.name, self.compute_weight(module))


T_module = TypeVar('T_module', bound=Module)

def weight_norm(module: T_module, name: str = 'weight', dim: int = 0) -> T_module:
    WeightNorm.apply(module, name, dim)
    return module



def remove_weight_norm(module: T_module, name: str = 'weight') -> T_module:
    r"""Remove the weight normalization reparameterization from a module.

    Args:
        module (Module): containing module
        name (str, optional): name of weight parameter

    Example:
        >>> m = weight_norm(nn.Linear(20, 40))
        >>> remove_weight_norm(m)
    """
    for k, hook in module._forward_pre_hooks.items():
        if isinstance(hook, WeightNorm) and hook.name == name:
            hook.remove(module)
            del module._forward_pre_hooks[k]
            return module

    raise ValueError(f"weight_norm of '{name}' not found in {module}")

class SDFModel(torch.nn.Module):
    def __init__(self, num_layers, skip_connections, latent_size, inner_dim=512, output_dim=1):
        """
        SDF model for multiple shapes.
        Args:
            input_dim: 128 for latent space + 3 points = 131
        """
        super(SDFModel, self).__init__()

        # Num layers of the entire network
        self.num_layers = num_layers 

        # If skip connections, add the input to one of the inner layers
        self.skip_connections = skip_connections

        self.latent_size = latent_size

        # Dimension of the input space (3D coordinates)
        dim_coords = 3 
        input_dim = self.latent_size + dim_coords

        # Copy input size to calculate the skip tensor size
        self.skip_tensor_dim = copy.copy(input_dim)

        # Compute how many layers are not Sequential
        num_extra_layers = 2 if (self.skip_connections and self.num_layers >= 8) else 1
        
        # Add sequential layers
        layers = []
        for _ in range(num_layers - num_extra_layers):
            layers.append(nn.Sequential(weight_norm(nn.Linear(input_dim, inner_dim)), nn.ReLU()))
            input_dim = inner_dim
        self.net = nn.Sequential(*layers)
        self.final_layer = nn.Sequential(nn.Linear(inner_dim, output_dim), nn.Tanh())
        self.skip_layer = nn.Sequential(nn.Linear(inner_dim, inner_dim - self.skip_tensor_dim), nn.ReLU())


    def forward(self, x):
        """
        Forward pass
        Args:
            x: input tensor of shape (batch_size, 131). It contains a stacked tensor [latent_code, samples].
        Returns:
            sdf: output tensor of shape (batch_size, 1)
        """      
        input_data = x.clone().detach()

        # Forward pass
        if self.skip_connections and self.num_layers >= 5:
            for i in range(3):
                x = self.net[i](x)
            x = self.skip_layer(x)
            x = torch.hstack((x, input_data))
            for i in range(self.num_layers - 5):
                x = self.net[3 + i](x)
            sdf = self.final_layer(x)
        else:
            if self.skip_connections:
                print('The network requires at least 5 layers to skip connections. Normal forward pass is used.')
            x = self.net(x)
            sdf = self.final_layer(x)
        return sdf


    def infer_latent_code(self, cfg, pointcloud, sdf_gt, writer, latent_code_initial):
        """Infer latent code from coordinates, their sdf, and a trained model."""

        latent_code = latent_code_initial.clone().detach().requires_grad_(True)
        
        optim = torch.optim.Adam([latent_code], lr=cfg['lr'])

        if cfg['lr_scheduler']:
            scheduler_latent = torch.optim.lr_scheduler.ReduceLROnPlateau(optim, mode='min', 
                                                    factor=cfg['lr_multiplier'], 
                                                    patience=cfg['patience'], 
                                                    threshold=0.001, threshold_mode='rel')

        best_loss = 1000000

        for epoch in tqdm(range(0, cfg['epochs'])):

            latent_code_tile = torch.tile(latent_code, (pointcloud.shape[0], 1))
            x = torch.hstack((latent_code_tile, pointcloud))

            optim.zero_grad()

            predictions = self(x)

            if cfg['clamp']:
                predictions = torch.clamp(predictions, -cfg['clamp_value'], cfg['clamp_value'])

            loss_value, l1, l2 = utils_deepsdf.SDFLoss_multishape(sdf_gt, predictions, x[:, :self.latent_size], sigma=cfg['sigma_regulariser'])
            loss_value.backward()

            if writer is not None:
                writer.add_scalar('Reconstruction loss', l1.data.cpu().numpy(), epoch)
                writer.add_scalar('Latent code loss', l2.data.cpu().numpy(), epoch)

            optim.step()

            if l1.detach().cpu().item() < best_loss:
                best_loss = l1.detach().cpu().item()
                best_latent_code = latent_code.clone()

            # step scheduler and store on tensorboard (optional)
            if cfg['lr_scheduler']:
                scheduler_latent.step(loss_value.item())
                if writer is not None:
                    writer.add_scalar('Learning rate', scheduler_latent._last_lr[0], epoch)

                if scheduler_latent._last_lr[0] < 1e-6:
                    print('Learning rate too small, stopping training')
                    break

            # logging
            if writer is not None:
                writer.add_scalar('Inference loss', loss_value.detach().cpu().item(), epoch)

        return best_latent_code