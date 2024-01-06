from typing import Optional, Callable, Any
import logging

import os
import torch
from torch.utils.data import TensorDataset
from pathlib import Path
import numpy as np

from beso.networks.scaler.scaler_class import Scaler
from beso.envs.dataloaders.trajectory_loader import (
    TrajectoryDataset,
    get_train_val_sliced,
)

ACTION_MEAN = np.array(
    [
        -0.089,
        0.712,
        -1.03,
        0.089,
        0.712,
        -1.03,
        -0.089,
        -0.712,
        1.03,
        0.089,
        -0.712,
        1.03,
    ]
)


def get_raisim_train_val(
    data_directory,
    train_fraction=0.9,
    random_seed=42,
    device="cpu",
    window_size=10,
    obs_dim=33,
    goal_conditional: Optional[str] = None,
    future_seq_len: Optional[int] = None,
    min_future_sep: int = 0,
    only_sample_tail: bool = False,
    only_sample_seq_end: bool = False,
    reduce_obs_dim: Optional[bool] = False,
    transform: Optional[Callable[[Any], Any]] = None,
):
    if goal_conditional is not None:
        assert goal_conditional in ["future", "onehot"]

    return get_train_val_sliced(
        RaisimTrajectoryDataset(
            data_directory,
            onehot_goals=(goal_conditional == "onehot"),
            reduce_obs_dim=reduce_obs_dim,
            obs_dim=obs_dim,
        ),
        train_fraction,
        random_seed,
        device,
        window_size,
        future_conditional=(goal_conditional == "future"),
        min_future_sep=min_future_sep,
        future_seq_len=future_seq_len,
        only_sample_tail=only_sample_tail,
        only_sample_seq_end=only_sample_seq_end,
        transform=transform,
    )


class RaisimTrajectoryDataset(TensorDataset, TrajectoryDataset):
    def __init__(
        self,
        data_directory: os.PathLike,
        device="cpu",
        onehot_goals=False,
        reduce_obs_dim=False,
        obs_dim=33,
    ):
        self.device = device
        self.data_directory = Path(data_directory)
        self.obs_dim = obs_dim
        logging.info("Data loading: started")
        data = np.load(self.data_directory, allow_pickle=True).item()
        self.observations = data["observations"]
        self.actions = data["actions"]
        self.terminals = data["terminals"]
        self.preprocess()
        self.split_data()

        self.observations = torch.from_numpy(self.observations).to(device).float()
        self.actions = torch.from_numpy(self.actions).to(device).float()
        self.masks = torch.from_numpy(self.masks).to(device).float()
        logging.info("Data loading: done")
        tensors = [self.observations, self.actions, self.masks]

        # The current values are in shape N x T x Dim, so all is good in the world.
        TensorDataset.__init__(self, *tensors)

    def get_seq_length(self, idx):
        return int(self.masks[idx].sum().item())

    def get_all_actions(self):
        result = []
        # mask out invalid actions
        for i in range(len(self.masks)):
            T = int(self.masks[i].sum().item())
            result.append(self.actions[i, :T, :])
        return torch.cat(result, dim=0)

    def get_all_observations(self):
        result = []
        # mask out invalid actions
        for i in range(len(self.masks)):
            T = int(self.masks[i].sum().item())
            result.append(self.observations[i, :T, :])
        return torch.cat(result, dim=0)

    def preprocess(self):
        self.observations = self.observations[:, :, : self.obs_dim]
        self.actions -= ACTION_MEAN

        # To split episodes correctly
        self.terminals[:, 0] = 1
        self.terminals[0, 0] = 0

    def split_data(self):
        # Flatten the first two dimensions
        obs_flat = self.observations.reshape(-1, self.observations.shape[-1])
        actions_flat = self.actions.reshape(-1, self.actions.shape[-1])
        terminals_flat = self.terminals.reshape(-1)

        # Find the indices where terminals is True (or 1)
        split_indices = np.where(terminals_flat == 1)[0]

        # Split the flattened observations and actions into sequences
        obs_splits = np.split(obs_flat, split_indices)
        actions_splits = np.split(actions_flat, split_indices)

        # Find the maximum length of the sequences
        max_len = max(split.shape[0] for split in obs_splits)

        # Pad the sequences and reshape them back to their original shape
        self.observations = self.pad_and_stack(obs_splits, max_len)
        self.actions = self.pad_and_stack(actions_splits, max_len)
        self.masks = self.create_masks(obs_splits, max_len)

    def pad_and_stack(self, splits, max_len):
        """Pad the sequences and stack them into a tensor"""
        return np.stack(
            [
                np.pad(
                    split, ((0, max_len - split.shape[0]), (0, 0)), mode="constant"
                ).reshape(-1, max_len, split.shape[1])
                for split in splits
            ]
        ).squeeze()

    def create_masks(self, splits, max_len):
        """Create masks for the sequences"""
        return np.stack(
            [
                np.pad(
                    np.ones(split.shape[0]),
                    (0, max_len - split.shape[0]),
                    mode="constant",
                )
                for split in splits
            ]
        )

    def __getitem__(self, idx):
        T = self.masks[idx].sum().int().item()
        return tuple(x[idx, :T] for x in self.tensors)


class PushTrajectorySequenceDataset(TensorDataset):
    def __init__(
        self,
        data_directory: os.PathLike,
        device="cpu",
        scale_data: bool = False,
        onehot_goals=False,
    ):
        self.device = device
        self.data_directory = Path(data_directory)
        logging.info("Multimodal loading: started")
        self.observations = np.load(
            self.data_directory / "multimodal_push_observations.npy"
        )
        self.actions = np.load(self.data_directory / "multimodal_push_actions.npy")
        self.masks = np.load(self.data_directory / "multimodal_push_masks.npy")
        self.observations = torch.from_numpy(self.observations).to(device).float()
        self.actions = torch.from_numpy(self.actions).to(device).float()
        self.masks = torch.from_numpy(self.masks).to(device).float()
        self.scaler = Scaler(self.observations, self.actions, scale_data, device)
        tensors = [self.observations, self.actions, self.masks]
        logging.info("Multimodal loading: done")
        # The current values are in shape N x T x Dim, so all is good in the world.
        if onehot_goals:
            tensors.append(self.goals)
        super.__init__(self, *tensors)

    def get_seq_length(self, idx):
        return int(self.masks[idx].sum().item())

    def get_all_actions(self):
        result = []
        # mask out invalid actions
        for i in range(len(self.masks)):
            T = int(self.masks[i].sum().item())
            result.append(self.actions[i, :T, :])
        return torch.cat(result, dim=0)

    def __getitem__(self, idx):
        T = self.masks[idx].sum().int().item()
        return tuple(x[idx, :T] for x in self.tensors)