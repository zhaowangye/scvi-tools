import numpy as np
import torch
import torch.nn.functional as F
import csv

from scvi.inference import Posterior
from scvi.inference import Trainer
from scvi.models.classifier import Classifier
from scvi.models.log_likelihood import compute_log_likelihood


class FishPosterior(Posterior):

    def ll(self, verbose=False):
        ll = compute_log_likelihood(self.model, self, mode="smFISH")
        if verbose:
            print("LL Fish: %.4f" % ll)
        return ll

    def show_spatial_expression(self, x_coord, y_coord, labels, color_by='scalar', title='spatial_expression.svg'):
        x_coord = x_coord.reshape(-1, 1)
        y_coord = y_coord.reshape(-1, 1)
        latent = np.concatenate((x_coord, y_coord), axis=1)
        self.show_t_sne(n_samples=1000, color_by=color_by, save_name=title, latent=latent, batch_indices=None,
                        labels=labels)


class TrainerFish(Trainer):
    r"""The VariationalInference class for the unsupervised training of an autoencoder.

    Args:
        :model: A model instance from class ``VAEF``
        :gene_dataset: A gene_dataset instance like ``CortexDataset()``
        :train_size: The train size, either a float between 0 and 1 or and integer for the number of training samples
         to use Default: ``0.8``.
        :\*\*kwargs: Other keywords arguments from the general Trainer class.

    Examples:
        >>> gene_dataset_seq = CortexDataset()
        >>> gene_dataset_fish = SmfishDataset()
        >>> vaef = VAEF(gene_dataset_seq.nb_genes, gene_dataset_fish.nb_genes,
        ... n_labels=gene_dataset.n_labels, use_cuda=True)

        >>> trainer = TrainerFish(gene_dataset_seq, gene_dataset_fish, vaef, train_size=0.5)
        >>> trainer.train(n_epochs=20, lr=1e-3)
    """
    default_metrics_to_monitor = ['ll']

    def __init__(self, model, gene_dataset_seq, gene_dataset_fish, train_size=0.8, test_size=None,
                 use_cuda=True, cl_ratio=0, n_epochs_even=1, n_epochs_kl=2000, n_epochs_cl=1, seed=0, warm_up=10,
                 scale=50, **kwargs):
        super(TrainerFish, self).__init__(model, gene_dataset_seq, use_cuda=use_cuda, **kwargs)
        self.kl = None
        self.cl_ratio = cl_ratio
        self.n_epochs_cl = n_epochs_cl
        self.n_epochs_even = n_epochs_even
        self.n_epochs_kl = n_epochs_kl
        self.weighting = 0
        self.kl_weight = 0
        self.classification_ponderation = 0
        self.warm_up = warm_up
        self.scale = scale

        self.train_seq, self.test_seq = self.train_test(self.model, gene_dataset_seq, train_size, test_size, seed)
        self.train_fish, self.test_fish = self.train_test(self.model, gene_dataset_fish,
                                                          train_size, test_size, seed, FishPosterior)
        self.all_fish_dataset = self.create_posterior(gene_dataset=gene_dataset_fish, type_class=FishPosterior)
        self.test_seq.to_monitor = ['ll']
        self.test_fish.to_monitor = ['ll']

    def train(self, n_epochs=20, lr=1e-3, weight_decay=1e-6, params=None):
        self.adversarial_cls = Classifier(self.model.n_latent, n_labels=self.model.n_batch, n_layers=3)
        if self.use_cuda:
            self.adversarial_cls.cuda()
        self.optimizer_cls = torch.optim.Adam(filter(lambda p: p.requires_grad, self.adversarial_cls.parameters()),
                                              lr=lr, weight_decay=weight_decay)
        super(TrainerFish, self).train(n_epochs=20, lr=1e-3, params=None)

    @property
    def posteriors_loop(self):
        return ['train_seq', 'train_fish']

    def loss(self, tensors_seq, tensors_fish):
        sample_batch, local_l_mean, local_l_var, batch_index, labels = tensors_seq
        reconst_loss, kl_divergence = self.model(sample_batch, local_l_mean, local_l_var, batch_index, mode="scRNA",
                                                 weighting=self.weighting)
        # If we want to add a classification loss
        # if self.cl_ratio != 0:
        #   reconst_loss += self.cl_ratio * F.cross_entropy(self.model.classify(sample_batch, mode="scRNA"),
        #                                                    labels.view(-1))
        loss = torch.mean(reconst_loss + self.kl_weight * kl_divergence)
        if len(tensors_fish) == 7:  # depending on whether or not we have spatial coordinates
            sample_batch_fish, local_l_mean, local_l_var, batch_index_fish, _, _, _ = tensors_fish
        else:
            sample_batch_fish, local_l_mean, local_l_var, batch_index_fish, _ = tensors_fish
        reconst_loss_fish, kl_divergence_fish = self.model(sample_batch_fish, local_l_mean, local_l_var,
                                                           batch_index_fish, mode="smFISH")
        loss_fish = torch.mean(reconst_loss_fish + self.kl_weight * kl_divergence_fish)
        loss = loss * sample_batch.size(0) + loss_fish * sample_batch_fish.size(0)
        loss /= (sample_batch.size(0) + sample_batch_fish.size(0))
        if self.epoch > self.warm_up:
            sample_batch, local_l_mean, local_l_var, batch_index, labels = tensors_seq
            z = self.model.sample_from_posterior_z(sample_batch, mode="scRNA")
            cls_loss = (self.scale * F.cross_entropy(self.adversarial_cls(z), torch.zeros_like(batch_index).view(-1)))
            if len(tensors_fish) == 7:  # depending on whether or not we have spatial coordinates
                sample_batch_fish, local_l_mean, local_l_var, batch_index_fish, _, _, _ = tensors_fish
            else:
                sample_batch_fish, local_l_mean, local_l_var, batch_index_fish, _ = tensors_fish
            z = self.model.sample_from_posterior_z(sample_batch, mode="smFISH")
            cls_loss += (self.scale * F.cross_entropy(self.adversarial_cls(z), torch.ones_like(batch_index).view(-1)))
            self.optimizer_cls.zero_grad()
            cls_loss.backward(retain_graph=True)
            self.optimizer_cls.step()
        else:
            cls_loss = 0
        return loss + loss_fish - cls_loss

    def on_epoch_begin(self):
        self.weighting = min(1, self.epoch / self.n_epochs_even)
        self.kl_weight = self.kl if self.kl is not None else min(1, self.epoch / self.n_epochs_kl)
        self.classification_ponderation = min(1, self.epoch / self.n_epochs_cl)

    def get_all_latent_and_expected_frequencies(self, save_imputed=False, file_name_imputation='imputed_values',
                                                save_shape_genes_by_cells=False, save_latent=False,
                                                file_name_latent='latent_space', mode='smFISH'):
        r"""
        :param save_imputed: True if the user wants to save the expected frequencies in a .csv file
        :param file_name_imputation: in the situation described above, this is the name of the file saved
        :param save_shape_genes_by_cells: if save-imputed is true this boolean determines if you want the
        expected frequencies to be saved as a genes by cells matrix or a cells by genes matrix
        :param save_latent: True if the user wants to save the latent space in a .csv file
        :param file_name_latent: in the situation described above, this is the name of the file saved
        :param mode: indicates on which dataset you want to retrieve information
        :return: a dictionnary of arrays which contains all the provided and inferred information for the whole dataset
        with the cells ordered the same way as in the original dataset expression matrix
        """
        self.model.eval()
        ret = {"latent": [], "expected_frequencies": [], "imputed_values": []}
        if mode == 'smFISH':
            for tensors in self.all_fish_dataset:
                sample_batch, local_l_mean, local_l_var, batch_index, label, x_coord, y_coord = tensors
                ret["latent"] += [self.model.sample_from_posterior_z(sample_batch, y=label, mode="smFISH")]
                ret["expected_frequencies"] += [self.model.get_sample_scale(sample_batch, mode="smFISH",
                                                                            batch_index=batch_index)]
                ret["imputed_values"] += [self.model.get_sample_rate_fish(sample_batch)]
            for key in ret.keys():
                if len(ret[key]) > 0:
                    ret[key] = np.array(torch.cat(ret[key]))
            ret['all_dataset'] = self.all_fish_dataset
        if mode == 'scRNA':
            for tensors in self.create_posterior(self.model, self.gene_dataset_seq):
                sample_batch, local_l_mean, local_l_var, batch_index, label = tensors
                ret["latent"] += [self.model.sample_from_posterior_z(sample_batch, y=label, mode="smFISH")]
                ret["expected_frequencies"] += [self.model.get_sample_scale(sample_batch, mode="smFISH",
                                                                            batch_index=batch_index)]
            for key in ret.keys():
                if len(ret[key]) > 0:
                    ret[key] = np.array(torch.cat(ret[key]))
            ret['all_dataset'] = self.create_posterior(self.model, self.gene_dataset_seq)

        if save_imputed:
            myfile = open(file_name_imputation, 'w')
            with myfile:
                writer = csv.writer(myfile)
                if save_shape_genes_by_cells:
                    writer.writerows(np.transpose(ret["expected_frequencies"]))
                else:
                    writer.writerows(ret["expected_frequencies"])
        if save_latent:
            myfile = open(file_name_latent, 'w')
            with myfile:
                writer = csv.writer(myfile)
                writer.writerows(ret["latent"])
        return ret
