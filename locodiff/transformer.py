import torch
import torch.nn as nn
from .utils import SinusoidalPosEmb


class DiffusionTransformer(nn.Module):
    def __init__(
        self,
        obs_dim,
        pred_obs_dim,
        skill_dim,
        act_dim,
        d_model,
        nhead,
        num_layers,
        T,
        T_cond,
        device,
        cond_mask_prob,
        dropout,
    ):
        super().__init__()
        self.obs_dim = obs_dim
        self.pred_obs_dim = pred_obs_dim
        self.act_dim = act_dim
        self.input_dim = obs_dim + act_dim
        self.d_model = d_model
        self.nhead = nhead
        self.num_layers = num_layers
        self.device = device
        self.cond_mask_prob = cond_mask_prob

        self.state_action_emb = nn.Linear(
            self.pred_obs_dim + self.act_dim, self.d_model
        )
        self.cond_state_emb = nn.Linear(self.obs_dim, self.d_model)
        self.sigma_emb = nn.Linear(1, self.d_model)
        self.vel_cmd_emb = nn.Linear(3, self.d_model)
        self.skill_emb = nn.Linear(skill_dim, self.d_model)

        self.pos_emb = (
            SinusoidalPosEmb(d_model)(torch.arange(T)).unsqueeze(0).to(device)
        )
        self.cond_pos_emb = (
            SinusoidalPosEmb(d_model)(torch.arange(T_cond + 3)).unsqueeze(0).to(device)
        )

        self.encoder = nn.Sequential(
            nn.Linear(d_model, 4 * d_model), nn.Mish(), nn.Linear(4 * d_model, d_model)
        )

        self.decoder = nn.TransformerDecoder(
            nn.TransformerDecoderLayer(
                d_model=self.d_model,
                nhead=self.nhead,
                dim_feedforward=4 * self.d_model,
                dropout=dropout,
                activation="gelu",
                batch_first=True,
                norm_first=True,
            ),
            num_layers=self.num_layers,
        )
        mask = self.generate_mask(T)
        self.register_buffer("mask", mask)

        self.ln_f = nn.LayerNorm(self.d_model)
        self.state_action_pred = nn.Linear(d_model, self.pred_obs_dim + self.act_dim)

        self.apply(self._init_weights)
        self.to(device)

    def _init_weights(self, module):
        ignore_types = (
            nn.Dropout,
            nn.TransformerDecoderLayer,
            nn.TransformerDecoder,
            nn.ModuleList,
            nn.Mish,
            nn.Sequential,
            DiffusionTransformer,
        )
        if isinstance(module, (nn.Linear, nn.Embedding)):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if isinstance(module, nn.Linear) and module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.MultiheadAttention):
            weight_names = [
                "in_proj_weight",
                "q_proj_weight",
                "k_proj_weight",
                "v_proj_weight",
            ]
            for name in weight_names:
                weight = getattr(module, name)
                if weight is not None:
                    torch.nn.init.normal_(weight, mean=0.0, std=0.02)

            bias_names = ["in_proj_bias", "bias_k", "bias_v"]
            for name in bias_names:
                bias = getattr(module, name)
                if bias is not None:
                    torch.nn.init.zeros_(bias)
        elif isinstance(module, nn.LayerNorm):
            torch.nn.init.zeros_(module.bias)
            torch.nn.init.ones_(module.weight)
        elif isinstance(module, ignore_types):
            pass
        else:
            raise RuntimeError("Unaccounted module {}".format(module))

    def get_optim_groups(self, weight_decay: float = 1e-3):
        """
        This long function is unfortunately doing something very simple and is being very defensive:
        We are separating out all parameters of the model into two buckets: those that will experience
        weight decay for regularization and those that won't (biases, and layernorm/embedding weights).
        We are then returning the PyTorch optimizer object.
        """

        # separate out all parameters to those that will and won't experience regularizing weight decay
        decay = set()
        no_decay = set()
        whitelist_weight_modules = (torch.nn.Linear, torch.nn.MultiheadAttention)
        blacklist_weight_modules = (torch.nn.LayerNorm, torch.nn.Embedding)
        for mn, m in self.named_modules():
            for pn, p in m.named_parameters():
                fpn = "%s.%s" % (mn, pn) if mn else pn  # full param name

                if pn.endswith("bias"):
                    # all biases will not be decayed
                    no_decay.add(fpn)
                elif pn.startswith("bias"):
                    # MultiheadAttention bias starts with "bias"
                    no_decay.add(fpn)
                elif pn.endswith("weight") and isinstance(m, whitelist_weight_modules):
                    # weights of whitelist modules will be weight decayed
                    decay.add(fpn)
                elif pn.endswith("weight") and isinstance(m, blacklist_weight_modules):
                    # weights of blacklist modules will NOT be weight decayed
                    no_decay.add(fpn)

        # validate that we considered every parameter
        param_dict = {pn: p for pn, p in self.named_parameters()}
        inter_params = decay & no_decay
        union_params = decay | no_decay
        assert (
            len(inter_params) == 0
        ), "parameters %s made it into both decay/no_decay sets!" % (str(inter_params),)
        assert (
            len(param_dict.keys() - union_params) == 0
        ), "parameters %s were not separated into either decay/no_decay set!" % (
            str(param_dict.keys() - union_params),
        )

        # create the pytorch optimizer object
        optim_groups = [
            {
                "params": [param_dict[pn] for pn in sorted(list(decay))],
                "weight_decay": weight_decay,
            },
            {
                "params": [param_dict[pn] for pn in sorted(list(no_decay))],
                "weight_decay": 0.0,
            },
        ]
        return optim_groups

    def forward(self, noised_action, sigma, data_dict, uncond=False):
        # constraint = kwargs["indicator"]
        # force_mask = kwargs.get("uncond", False)
        # constraint = self.mask_cond(constraint, force_mask=force_mask)
        # cond = torch.cat([cond, constraint], dim=-1)

        # embeddings
        input_emb = self.state_action_emb(noised_action)
        sigma_emb = self.sigma_emb(sigma.view(-1, 1, 1).log() / 4)
        cond_emb = self.cond_state_emb(data_dict["obs"])

        vel_cmd = data_dict["vel_cmd"]
        vel_cmd_emb = self.vel_cmd_emb(vel_cmd).unsqueeze(1)

        skill = data_dict["skill"]
        # skill = self.mask_cond(skill, uncond)
        skill_emb = self.skill_emb(skill).unsqueeze(1)

        cond = torch.cat([sigma_emb, vel_cmd_emb, skill_emb, cond_emb], dim=1)
        cond += self.cond_pos_emb
        cond = self.encoder(cond)

        input_emb += self.pos_emb
        x = self.decoder(tgt=input_emb, memory=cond, tgt_mask=self.mask)
        x = self.ln_f(x)
        out = self.state_action_pred(x)

        return out

    def generate_mask(self, x):
        mask = (torch.triu(torch.ones(x, x)) == 1).transpose(0, 1)
        mask = (
            mask.float()
            .masked_fill(mask == 0, float("-inf"))
            .masked_fill(mask == 1, float(0.0))
        )
        return mask

    def mask_cond(self, cond, force_mask=False):
        if force_mask:
            return torch.full_like(cond, 0)
        elif self.training and self.cond_mask_prob > 0:
            mask = (torch.rand_like(cond[..., 0:1]) > self.cond_mask_prob).float()
            mask = mask.expand_as(cond)
            cond[mask == 0] = 0
            return cond
        else:
            return cond

    def get_params(self):
        return self.parameters()

    def detach_all(self):
        for name, param in self.named_parameters():
            param.detach_()


if __name__ == "__main__":
    torch.manual_seed(0)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = DiffusionTransformer(
        obs_dim=33,
        act_dim=12,
        d_model=128,
        nhead=4,
        num_layers=4,
        T=6,
        T_cond=4,
        device=device,
    )
    x = torch.randn(1, 6, 45).to(device)
    cond = torch.randn(1, 4, 45).to(device)
    goal = torch.randn(1, 1).to(device)
    sigma = torch.tensor([1]).to(device)
    model(x, cond, sigma)
