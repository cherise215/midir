import torch.nn as nn
import numpy as np

from model.networks.dvf_nets import SiameseNet, UNet
from model.networks.ffd_nets import SiameseNetFFD, FFDNet
from model.transformations import BSplineFFDTransform, BSplineFFDTransformPoly, DVFTransform


class DLRegModel(nn.Module):
    def __init__(self, params):
        super(DLRegModel, self).__init__()

        self.params = params

        # initialise recording variables
        self.epoch_num = 0
        self.iter_num = 0
        self.is_best = False
        self.best_metric_result = 0

        self._set_network()
        self._set_transformation_model()


    def _set_network(self):
        if self.params.network == "SiameseNetDVF":
            self.network = SiameseNet()

        elif self.params.network == "UNetDVF":
            self.network = UNet(dim=self.params.dim,
                                   enc_channels=self.params.enc_channels,
                                   dec_channels=self.params.dec_channels,
                                   out_channels=self.params.out_channels
                                   )

        elif self.params.network == "SiameseNetFFD":
            self.network = SiameseNetFFD()

        elif self.params.network == "FFDNet":
            self.network = FFDNet(dim=self.params.dim,
                                  img_size=self.params.crop_size,
                                  cpt_spacing=self.params.ffd_sigma,
                                  enc_channels=self.params.enc_channels,
                                  out_channels=self.params.out_channels
                                  )
        else:
            raise ValueError("Model: Network not recognised")


    def _set_transformation_model(self):
        if self.params.transformation == "DVF":
            self.transform = DVFTransform()

        elif self.params.transformation == "FFD":
            self.transform = BSplineFFDTransform(dim=self.params.dim,
                                                 img_size=self.params.crop_size,
                                                 sigma=self.params.ffd_sigma)
        else:
            raise ValueError("Model: Transformation model not recognised")


    def update_best_model(self, metric_results):
        metric_results_mean = np.mean([metric_results[metric] for metric in self.params.best_metrics])

        if self.epoch_num + 1 == self.params.val_epochs:
            # initialise for the first validation
            self.best_metric_result = metric_results_mean
        else:
            if metric_results_mean < self.best_metric_result:
                self.is_best = True
                self.best_metric_result = metric_results_mean


    def forward(self, target, source):
        net_out = self.network(target, source)
        dvf = self.transform(net_out)
        return dvf
