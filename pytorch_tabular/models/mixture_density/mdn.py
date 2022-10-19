# Pytorch Tabular
# Author: Manu Joseph <manujoseph@gmail.com>
# For license information, see LICENSE.TXT
"""Mixture Density Models"""
import logging
from typing import Dict, Optional

import torch
import torch.nn as nn
from omegaconf import DictConfig, OmegaConf
from torch import Tensor

from pytorch_tabular import models
from pytorch_tabular.config.config import ModelConfig
from pytorch_tabular.models.common.heads import blocks
from pytorch_tabular.tabular_model import getattr_nested

from ..base_model import BaseModel, safe_merge_config

try:
    import wandb

    WANDB_INSTALLED = True
except ImportError:
    WANDB_INSTALLED = False
logger = logging.getLogger(__name__)


class MDNModel(BaseModel):
    def __init__(self, config: DictConfig, **kwargs):
        assert (
            "inferred_config" in kwargs
        ), "inferred_config not found in initialization arguments"
        self.inferred_config = kwargs["inferred_config"]
        assert config.task == "regression", "MDN is only implemented for Regression"
        super().__init__(config, **kwargs)
        assert self.hparams.output_dim == 1, "MDN is not implemented for multi-targets"
        if config.target_range is not None:
            logger.warning("MDN does not use target range. Ignoring it.")

    def _get_head_from_config(self):
        _head_callable = getattr(blocks, self.hparams.head)
        self.hparams.head_config.input_dim = self.backbone.output_dim
        return _head_callable(
            config=_head_callable._config_template(**self.hparams.head_config),
        )  # output_dim auto-calculated from other configs

    def _build_network(self):
        # Backbone
        callable, config = (
            self.hparams.backbone_config_class,
            self.hparams.backbone_config_params,
        )
        try:
            callable = getattr(models, callable)
        except ModuleNotFoundError as e:
            logger.error(
                "`config class` in `backbone_config` is not valid. The config class should be a valid module path from `models`. e.g. `ft_transformer.FTTransformerConfig`."
            )
            raise e
        assert issubclass(
            callable, ModelConfig
        ), "`config_class` should be a subclass of `ModelConfig`"
        backbone_config = callable(**config)
        backbone_callable = getattr_nested(
            backbone_config._module_src, backbone_config._backbone_name
        )
        # Merging the config and inferred config
        backbone_config = safe_merge_config(
            OmegaConf.structured(backbone_config), self.inferred_config
        )
        self.backbone = backbone_callable(backbone_config)
        # Adding the last layer

        self.head = self._get_head_from_config()

    def compute_head(self, x: Tensor):
        pi, sigma, mu = self.head(x)
        return {"pi": pi, "sigma": sigma, "mu": mu, "backbone_features": x}

    def predict(self, x: Dict):
        ret_value = self.forward(x)
        return self.head.generate_point_predictions(
            ret_value["pi"], ret_value["sigma"], ret_value["mu"]
        )

    def sample(self, x: Dict, n_samples: Optional[int] = None, ret_model_output=False):
        ret_value = self.forward(x)
        samples = self.head.generate_samples(
            ret_value["pi"], ret_value["sigma"], ret_value["mu"], n_samples
        )
        if ret_model_output:
            return samples, ret_value
        else:
            return samples

    def calculate_loss(self, y, pi, sigma, mu, tag="train"):
        # NLL Loss
        log_prob = self.head.log_prob(pi, sigma, mu, y)
        loss = torch.mean(-log_prob)
        if self.head.hparams.weight_regularization is not None:
            sigma_l1_reg = 0
            pi_l1_reg = 0
            mu_l1_reg = 0
            if self.head.hparams.lambda_sigma > 0:
                # Weight Regularization Sigma
                sigma_params = torch.cat(
                    [x.view(-1) for x in self.head.sigma.parameters()]
                )
                sigma_l1_reg = self.head.hparams.lambda_sigma * torch.norm(
                    sigma_params, self.head.hparams.weight_regularization
                )
            if self.head.hparams.lambda_pi > 0:
                pi_params = torch.cat([x.view(-1) for x in self.head.pi.parameters()])
                pi_l1_reg = self.head.hparams.lambda_pi * torch.norm(
                    pi_params, self.head.hparams.weight_regularization
                )
            if self.head.hparams.lambda_mu > 0:
                mu_params = torch.cat([x.view(-1) for x in self.head.mu.parameters()])
                mu_l1_reg = self.head.hparams.lambda_mu * torch.norm(
                    mu_params, self.head.hparams.weight_regularization
                )

            loss = loss + sigma_l1_reg + pi_l1_reg + mu_l1_reg
        self.log(
            f"{tag}_loss",
            loss,
            on_epoch=(tag == "valid") or (tag == "test"),
            on_step=(tag == "train"),
            # on_step=False,
            logger=True,
            prog_bar=True,
        )
        return loss

    def training_step(self, batch, batch_idx):
        y = batch["target"]
        ret_value = self(batch)
        loss = self.calculate_loss(
            y, ret_value["pi"], ret_value["sigma"], ret_value["mu"], tag="train"
        )
        if self.head.hparams.speedup_training:
            pass
        else:
            y_hat = self.head.generate_point_predictions(
                ret_value["pi"], ret_value["sigma"], ret_value["mu"]
            )
            _ = self.calculate_metrics(y, y_hat, tag="train")
        return loss

    def validation_step(self, batch, batch_idx):
        y = batch["target"]
        ret_value = self(batch)
        _ = self.calculate_loss(
            y, ret_value["pi"], ret_value["sigma"], ret_value["mu"], tag="valid"
        )
        y_hat = self.head.generate_point_predictions(
            ret_value["pi"], ret_value["sigma"], ret_value["mu"]
        )
        _ = self.calculate_metrics(y, y_hat, tag="valid")
        return y_hat, y, ret_value

    def test_step(self, batch, batch_idx):
        y = batch["target"]
        ret_value = self(batch)
        _ = self.calculate_loss(
            y, ret_value["pi"], ret_value["sigma"], ret_value["mu"], tag="test"
        )
        y_hat = self.head.generate_point_predictions(
            ret_value["pi"], ret_value["sigma"], ret_value["mu"]
        )
        _ = self.calculate_metrics(y, y_hat, tag="test")
        return y_hat, y

    def validation_epoch_end(self, outputs) -> None:
        do_log_logits = (
            hasattr(self.hparams, "log_logits")
            and self.hparams.log_logits
            and self.hparams.log_target == "wandb"
            and WANDB_INSTALLED
        )
        pi = [
            nn.functional.gumbel_softmax(
                output[2]["pi"], tau=self.head.hparams.softmax_temperature, dim=-1
            )
            for output in outputs
        ]
        pi = torch.cat(pi).detach().cpu()
        for i in range(self.head.hparams.num_gaussian):
            self.log(
                f"mean_pi_{i}",
                pi[:, i].mean(),
                on_epoch=True,
                on_step=False,
                logger=True,
                prog_bar=False,
            )

        mu = [output[2]["mu"] for output in outputs]
        mu = torch.cat(mu).detach().cpu()
        for i in range(self.head.hparams.num_gaussian):
            self.log(
                f"mean_mu_{i}",
                mu[:, i].mean(),
                on_epoch=True,
                on_step=False,
                logger=True,
                prog_bar=False,
            )

        sigma = [output[2]["sigma"] for output in outputs]
        sigma = torch.cat(sigma).detach().cpu()
        for i in range(self.head.hparams.num_gaussian):
            self.log(
                f"mean_sigma_{i}",
                sigma[:, i].mean(),
                on_epoch=True,
                on_step=False,
                logger=True,
                prog_bar=False,
            )
        if do_log_logits:
            logits = [output[0] for output in outputs]
            logits = torch.cat(logits).detach().cpu()
            fig = self.create_plotly_histogram(logits.unsqueeze(1), "logits")
            wandb.log(
                {
                    "valid_logits": fig,
                    "global_step": self.global_step,
                },
                commit=False,
            )
            if self.head.hparams.log_debug_plot:
                fig = self.create_plotly_histogram(
                    pi, "pi", bin_dict=dict(start=0.0, end=1.0, size=0.1)
                )
                wandb.log(
                    {
                        "valid_pi": fig,
                        "global_step": self.global_step,
                    },
                    commit=False,
                )

                fig = self.create_plotly_histogram(mu, "mu")
                wandb.log(
                    {
                        "valid_mu": fig,
                        "global_step": self.global_step,
                    },
                    commit=False,
                )

                fig = self.create_plotly_histogram(sigma, "sigma")
                wandb.log(
                    {
                        "valid_sigma": fig,
                        "global_step": self.global_step,
                    },
                    commit=False,
                )


# class CategoryEmbeddingMDN(MDNModel):
#     def __init__(self, config: DictConfig, **kwargs):
#         super().__init__(config, **kwargs)

#     def _build_network(self):
#         # Backbone
#         self.backbone = CategoryEmbeddingBackbone(self.hparams)
#         # Adding the last layer
#         self.hparams.mdn_config.input_dim = self.backbone.output_dim
#         self.mdn = MixtureDensityHead(self.hparams.mdn_config)


# class NODEMDN(MDNModel):
#     def __init__(self, config: DictConfig, **kwargs):
#         super().__init__(config, **kwargs)

#     def subset(self, x):
#         return x[..., :].mean(dim=-2)

#     def _build_network(self):
#         self.hparams.node_input_dim = (
#             self.hparams.continuous_dim + self.hparams.categorical_dim
#         )
#         backbone = NODEBackbone(self.hparams)
#         # average first n channels of every tree, where n is the number of output targets for regression
#         # and number of classes for classification

#         output_response = Lambda(self.subset)
#         self.backbone = nn.Sequential(backbone, output_response)
#         # Adding the last layer
#         self.hparams.mdn_config.input_dim = backbone.output_dim
#         setattr(self.backbone, "output_dim", backbone.output_dim)
#         self.mdn = MixtureDensityHead(self.hparams.mdn_config)


# class AutoIntMDN(MDNModel):
#     def __init__(self, config: DictConfig, **kwargs):
#         super().__init__(config, **kwargs)

#     def _build_network(self):
#         # Backbone
#         self.backbone = AutoIntBackbone(self.hparams)
#         # Adding the last layer
#         self.hparams.mdn_config.input_dim = self.backbone.output_dim
#         self.mdn = MixtureDensityHead(self.hparams.mdn_config)
