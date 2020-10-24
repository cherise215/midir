import math
import numpy as np

import torch
from torch import nn as nn
import torch.nn.functional as F

from model.loss import window_func
from utils.image import normalise_intensity
from utils.misc import param_dim_setup
from utils.image import avg_filtering


class MILossGaussian(nn.Module):
    """
    Mutual information loss using Gaussian kernel in KDE
    Adapting AirLab implementation to handle batches (& changing input):
    https://github.com/airlab-unibas/airlab/blob/80c9d487c012892c395d63c6d937a67303c321d1/airlab/loss/pairwise.py#L275
    """
    def __init__(self,
                 vmin=0.0,
                 vmax=1.0,
                 num_bins=64,
                 roi=False,
                 roi_threshold=0.0001,
                 normalised=True
                 ):
        super(MILossGaussian, self).__init__()

        self.vmin = vmin
        self.vmax = vmax

        # set the std of Gaussian kernel so that FWHM is one bin width
        bin_width = (vmax - vmin) / num_bins
        self.sigma = bin_width * (1/(2 * math.sqrt(2 * math.log(2))))

        # set bin edges
        self.num_bins = num_bins
        self.bins = torch.linspace(self.vmin, self.vmax, self.num_bins, requires_grad=False).unsqueeze(1)

        # roi masking
        self.roi = roi
        self.roi_threshold = roi_threshold

        self.normalised = normalised

    def _compute_joint_prob(self, x, y):
        """
        Compute joint distribution and entropy
        Input shapes (N, 1, H*W*D)
        """
        # cast bins
        self.bins = self.bins.type_as(x)

        # calculate Parzen window function response (N, #bins, H*W*D)
        win_x = torch.exp(-(x - self.bins) ** 2 / (2 * self.sigma ** 2)) / math.sqrt(2 * math.pi) * self.sigma
        win_y = torch.exp(-(y - self.bins) ** 2 / (2 * self.sigma ** 2)) / math.sqrt(2 * math.pi) * self.sigma

        # calculate joint histogram batch
        hist_joint = win_x.bmm(win_y.transpose(1, 2))  # (N, #bins, #bins)

        # normalise joint histogram to get joint distribution
        hist_norm = hist_joint.flatten(start_dim=1, end_dim=-1).sum(dim=1) + 1e-10
        p_joint = hist_joint / hist_norm.view(-1, 1, 1)  # (N, #bins, #bins) / (N, 1, 1)

        return p_joint

    def forward(self, x, y):
        """
        Calculate (Normalised) Mutual Information Loss.

        Args:
            x: (torch.Tensor, size (N, dim, *size)
            y: (torch.Tensor, size (N, dim, *size)

        Returns:
            (Normalise)MI: (scalar)
        """
        # Handle roi mask (each loss may handle roi masking differently, e.g. LNCC needs the spatial structure)
        #  (NOTE: this doesn't work for 2D as each slice could have different number of elements selected,
        #  use a bounding box mask to select the same region across slices instead)
        if self.roi:
            img_avg = (x + y) / 2
            img_avg_smooth = avg_filtering(img_avg, filter_size=7)  # extend ROI
            mask = img_avg_smooth > self.roi_threshold
            x = torch.masked_select(x, mask).unsqueeze(0).unsqueeze(0)
            y = torch.masked_select(y, mask).unsqueeze(0).unsqueeze(0)

        else:
            # same shape as masked
            x = x.flatten(start_dim=2, end_dim=-1)
            y = y.flatten(start_dim=2, end_dim=-1)

        # compute joint distribution
        p_joint = self._compute_joint_prob(x, y)

        # marginalise the joint distribution to get marginal distributions
        # batch size in dim0, target bins in dim1, source bins in dim2
        p_x = torch.sum(p_joint, dim=2)
        p_y = torch.sum(p_joint, dim=1)

        # calculate entropy
        ent_x = - torch.sum(p_x * torch.log(p_x + 1e-10), dim=1)  # (N,1)
        ent_y = - torch.sum(p_y * torch.log(p_y + 1e-10), dim=1)  # (N,1)
        ent_joint = - torch.sum(p_joint * torch.log(p_joint + 1e-10), dim=(1, 2))  # (N,1)

        if self.normalised:
            return -torch.mean((ent_x + ent_y) / ent_joint)
        else:
            return -torch.mean(ent_x + ent_y - ent_joint)


class LNCCLoss(nn.Module):
    """
    Local Normalized Cross Correlation loss
    Modified on VoxelMorph implementation:
    https://github.com/voxelmorph/voxelmorph/blob/5273132227c4a41f793903f1ae7e27c5829485c8/voxelmorph/torch/losses.py#L7
    """
    def __init__(self, window_size=7):
        super(LNCCLoss, self).__init__()
        self.window_size = window_size

    def forward(self, tar, src):
        # products and squares
        tar2 = tar * tar
        src2 = src * src
        tar_src = tar * src

        # set window size
        dim = tar.dim() - 2
        window_size = param_dim_setup(self.window_size, dim)

        # summation filter for convolution
        sum_filt = torch.ones(1, 1, *window_size).type_as(tar)

        # set stride and padding
        stride = (1,) * dim
        padding = tuple([math.floor(window_size[i]/2) for i in range(dim)])

        # get convolution function of the correct dimension
        conv_fn = getattr(F, f'conv{dim}d')

        # summing over window by convolution
        tar_sum = conv_fn(tar, sum_filt, stride=stride, padding=padding)
        src_sum = conv_fn(src, sum_filt, stride=stride, padding=padding)
        tar2_sum = conv_fn(tar2, sum_filt, stride=stride, padding=padding)
        src2_sum = conv_fn(src2, sum_filt, stride=stride, padding=padding)
        tar_src_sum = conv_fn(tar_src, sum_filt, stride=stride, padding=padding)

        window_num_points = np.prod(window_size)
        mu_tar = tar_sum / window_num_points
        mu_src = src_sum / window_num_points

        cov = tar_src_sum - mu_src * tar_sum - mu_tar * src_sum + mu_tar * mu_src * window_num_points
        tar_var = tar2_sum - 2 * mu_tar * tar_sum + mu_tar * mu_tar * window_num_points
        src_var = src2_sum - 2 * mu_src * src_sum + mu_src * mu_src * window_num_points

        lncc = cov * cov / (tar_var * src_var + 1e-5)

        return -torch.mean(lncc)


class MILossBSpline(nn.Module):
    # (Not in use due to memory issue with polynomial B-spline)
    def __init__(self,
                 target_min=0.0,
                 target_max=1.0,
                 source_min=0.0,
                 source_max=1.0,
                 num_bins_target=32,
                 num_bins_source=32,
                 c_target=1.0,
                 c_source=1.0,
                 normalised=True,
                 window="cubic_bspline",
                 debug=False):

        super().__init__()
        self.debug = debug
        self.normalised = normalised
        self.window = window

        self.target_min = target_min
        self.target_max = target_max
        self.source_min = source_min
        self.source_max = source_max

        # set bin edges (assuming image intensity is normalised to the range)
        self.bin_width_target = (target_max - target_min) / num_bins_target
        self.bin_width_source = (source_max - source_min) / num_bins_source

        bins_target = torch.arange(num_bins_target) * self.bin_width_target + target_min
        bins_source = torch.arange(num_bins_source) * self.bin_width_source + source_min

        self.bins_target = bins_target.unsqueeze(1).float()  # (N, 1, #bins, H*W)
        self.bins_source = bins_source.unsqueeze(1).float()  # (N, 1, #bins, H*W)

        self.bins_target.requires_grad_(False)
        self.bins_source.requires_grad_(False)


        # determine kernel width controlling parameter (eps)
        # to satisfy the partition of unity constraint, eps can be no larger than bin_width
        # and can only be reduced by integer factor to increase kernel support,
        # cubic B-spline function has support of 4*eps, hence maximum number of bins a sample can affect is 4c
        self.eps_target = c_target * self.bin_width_target
        self.eps_source = c_source * self.bin_width_source

        if self.debug:
            self.histogram_joint = None
            self.p_joint = None
            self.p_target = None
            self.p_source = None

    @staticmethod
    def _parzen_window_1d(x, window="cubic_bspline"):
        if window == "cubic_bspline":
            return window_func.cubic_bspline_torch(x)
        elif window == "rectangle":
            return window_func.rect_window_torch(x)
        else:
            raise Exception("Window function not recognised.")

    def forward(self,
                target,
                source):
        """
        Calculate (Normalised) Mutual Information Loss.

        Args:
            target: (torch.Tensor, size (N, dim, *size)
            source: (torch.Tensor, size (N, dim, *size)

        Returns:
            (Normalise)MI: (scalar)
        """

        """pre-scripts"""
        # normalise intensity for histogram calculation
        target = normalise_intensity(target[:, 0, ...],
                                     mode='minmax', min_out=self.target_min, max_out=self.target_max).unsqueeze(1)
        source = normalise_intensity(source[:, 0, ...],
                                     mode='minmax', min_out=self.source_min, max_out=self.source_max).unsqueeze(1)

        # flatten images to (N, 1, prod(*size))
        target = target.view(target.size()[0], target.size()[1], -1)
        source = source.view(source.size()[0], source.size()[1], -1)

        """histograms"""
        # bins to device
        self.bins_target = self.bins_target.type_as(target)
        self.bins_source = self.bins_source.type_as(source)

        # calculate Parzen window function response
        D_target = (self.bins_target - target) / self.eps_target
        D_source = (self.bins_source - source) / self.eps_source
        W_target = self._parzen_window_1d(D_target, window=self.window)  # (N, #bins, H*W)
        W_source = self._parzen_window_1d(D_source, window=self.window)  # (N, #bins, H*W)

        # calculate joint histogram (using batch matrix multiplication)
        hist_joint = W_target.bmm(W_source.transpose(1, 2))  # (N, #bins, #bins)
        hist_joint /= self.eps_target * self.eps_source

        """distributions"""
        # normalise joint histogram to get joint distribution
        hist_norm = hist_joint.flatten(start_dim=1, end_dim=-1).sum(dim=1)  # normalisation factor per-image,
        p_joint = hist_joint / hist_norm.view(-1, 1, 1)  # (N, #bins, #bins) / (N, 1, 1)

        # marginalise the joint distribution to get marginal distributions
        # batch size in dim0, target bins in dim1, source bins in dim2
        p_target = torch.sum(p_joint, dim=2)
        p_source = torch.sum(p_joint, dim=1)

        """entropy"""
        # calculate entropy
        ent_target = - torch.sum(p_target * torch.log(p_target + 1e-12), dim=1)  # (N,1)
        ent_source = - torch.sum(p_source * torch.log(p_source + 1e-12), dim=1)  # (N,1)
        ent_joint = - torch.sum(p_joint * torch.log(p_joint + 1e-12), dim=(1, 2))  # (N,1)

        """debug mode: store bins, histograms & distributions"""
        if self.debug:
            self.histogram_joint = hist_joint
            self.p_joint = p_joint
            self.p_target = p_target
            self.p_source = p_source

        # return (unnormalised) mutual information or normalised mutual information (NMI)
        if self.normalised:
            return -torch.mean((ent_target + ent_source) / ent_joint)
        else:
            return -torch.mean(ent_target + ent_source - ent_joint)