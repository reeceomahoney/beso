import hydra
from torch import nn

"""
Wrappers for the score-based models based on Karras et al. 2022
They are used to get improved scaling of different noise levels, which
improves training stability and model performance 

Code is adapted from:

https://github.com/crowsonkb/k-diffusion/blob/master/k_diffusion/layers.py
"""


class GCDenoiser(nn.Module):
    """
    A Karras et al. preconditioner for denoising diffusion models.

    Args:
        inner_model: The inner model used for denoising.
        sigma_data: The data sigma for scalings (default: 1.0).
    """

    def __init__(self, inner_model, sigma_data):
        super().__init__()
        self.inner_model = hydra.utils.instantiate(inner_model)
        self.sigma_data = sigma_data

    def get_scalings(self, sigma):
        """
        Compute the scalings for the denoising process.

        Args:
            sigma: The input sigma.
        Returns:
            The computed scalings for skip connections, output, and input.
        """
        c_skip = self.sigma_data**2 / (sigma**2 + self.sigma_data**2)
        c_out = sigma * self.sigma_data / (sigma**2 + self.sigma_data**2) ** 0.5
        c_in = 1 / (sigma**2 + self.sigma_data**2) ** 0.5
        return c_skip, c_out, c_in

    def loss(self, x, cond, noise, sigma, **kwargs):
        """
        Compute the loss for the denoising process.

        Args:
            state_action: The input state_action.
            goal: The input goal.
            noise: The input noise.
            sigma: The input sigma.
            **kwargs: Additional keyword arguments.
        Returns:
            The computed loss.
        """
        noised_input = x + noise * sigma.view(-1, 1, 1)

        c_skip, c_out, c_in = [x.view(-1, 1, 1) for x in self.get_scalings(sigma)]
        model_output = self.inner_model(noised_input * c_in, cond, sigma, **kwargs)
        target = (x - c_skip * noised_input) / c_out

        loss = (model_output - target).pow(2).mean()
        return loss

    def forward(self, x_t, cond, sigma, **kwargs):
        """
        Perform the forward pass of the denoising process.

        Args:
            state_action: The input state_action.
            goal: The input goal.
            sigma: The input sigma.
            **kwargs: Additional keyword arguments.

        Returns:
            The output of the forward pass.
        """

        c_skip, c_out, c_in = [x.view(-1, 1, 1) for x in self.get_scalings(sigma)]
        return (
            self.inner_model(x_t * c_in, cond, sigma, **kwargs) * c_out + x_t * c_skip
        )

    def get_params(self):
        return self.inner_model.parameters()
