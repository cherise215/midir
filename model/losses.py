"""Loss functions"""

import torch
import torch.nn as nn

from model.submodules import resample_transform

##############################################################################################
# --- Huber loss --- #
##############################################################################################

def huber_loss_spatial(flow):
    """
    Calculate approximated spatial Huber loss
    Args:
        flow: optical flow estimated, Tensor of shape (N, 2, H, W)

    Returns:
        spatial_huber_loss: the loss value

    """
    eps = 0.01  # numerical stability

    # magnitude of the flow
    flow_norm = torch.norm(flow, dim=1)  # (N, H, W)

    # spatial finite derivative, 1st order
    flow_norm_dx = flow_norm[:, 1:, :] - flow_norm[:, :-1, :]  # (N, H-1, W)
    flow_norm_dy = flow_norm[:, :, 1:] - flow_norm[:, :, :-1]  # (N, H, W-1)

    # calculate the huber loss step by step
    # drop 1 on one dimension to match shapes to (N, H-1, W-1)
    spatial_huber_loss = flow_norm_dx[:, :, :-1] ** 2 + flow_norm_dy[:, :-1, :] ** 2  # ^2 + ^2: (N, H-1, W-1)
    spatial_huber_loss = torch.sqrt( eps + torch.sum( torch.sum(spatial_huber_loss, dim=1), dim=1) )  # sqrt(sum over space)
    spatial_huber_loss = torch.mean(spatial_huber_loss)  # mean over time (batch)

    return spatial_huber_loss


def huber_loss_temporal(flow):
    """
    Calculate approximated temporal Huber loss

    Args:
        flow: optical flow estimated, Tensor of shape (N, 2, H, W)

    Returns:
        temporal_huber_loss: the loss value

    """
    eps = 0.01  # numerical stability

    # magnitude of the flow
    flow_norm = torch.norm(flow, dim=1)  # (N, H, W)

    # temporal finite derivatives, 1st order
    flow_norm_dt = flow_norm[1:, :, :] - flow_norm[:-1, :, :]
    temporal_huber_loss = torch.sqrt(eps + torch.sum(flow_norm_dt ** 2))
    return temporal_huber_loss


def huber_loss_fn(flow, target, source, params):
    """
    Unsupervised loss function with optional pseudo-Huber loss as regularisation

    Args:
        flow: (Tensor, shape Nx2xHxW) predicted flow from target image to source image
        target: (Tensor, shape NxchxHxW) target image
        source: (Tensor, shape NxchxHxW) source image

    Returns:
        loss
        losses: (dict) dictionary of individual loss term after weighting
    """

    # warp the source image towards target using grid resample
    # i.e. flow is from target to source
    warped_source = resample_transform(source, flow)

    mse = nn.MSELoss()
    mse_loss = mse(target, warped_source)
    smooth_loss = params.huber_spatial * huber_loss_spatial(flow) + params.huber_temporal * huber_loss_temporal(flow)

    loss = mse_loss + smooth_loss
    losses = {'mse': mse_loss,  'smooth_loss': smooth_loss}

    return loss, losses

