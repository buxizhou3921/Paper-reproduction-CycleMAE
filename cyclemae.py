import torch
import torch.nn as nn
from timm.models.vision_transformer import PatchEmbed, Block
from util.pos_embed import get_2d_sincos_pos_embed


class Decoder(nn.Module):
    def __init__(self, embed_dim, patch_size, num_patches, in_chans=3, decoder_embed_dim=512, decoder_depth=8,
                 decoder_num_heads=16,
                 mlp_ratio=4., norm_layer=nn.LayerNorm, norm_pix_loss=False):
        super().__init__()
        self.num_patches = num_patches
        self.decoder_embed = nn.Linear(embed_dim, decoder_embed_dim, bias=True)

        self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))

        self.decoder_pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, decoder_embed_dim),
                                              requires_grad=False)  # fixed sin-cos embedding

        self.decoder_blocks = nn.ModuleList([
            Block(decoder_embed_dim, decoder_num_heads, mlp_ratio, qkv_bias=True, qk_scale=None, norm_layer=norm_layer)
            for i in range(decoder_depth)])

        self.decoder_norm = norm_layer(decoder_embed_dim)
        self.decoder_pred = nn.Linear(decoder_embed_dim, patch_size ** 2 * in_chans, bias=True)  # decoder to patch
        self.initialize_weights()

    def initialize_weights(self):
        decoder_pos_embed = get_2d_sincos_pos_embed(self.decoder_pos_embed.shape[-1], int(self.num_patches ** .5),
                                                    cls_token=True)
        self.decoder_pos_embed.data.copy_(torch.from_numpy(decoder_pos_embed).float().unsqueeze(0))

        torch.nn.init.normal_(self.mask_token, std=.02)
        # initialize nn.Linear and nn.LayerNorm
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x, ids_restore):
        # embed tokens
        x = self.decoder_embed(x)

        # append mask tokens to sequence
        mask_tokens = self.mask_token.repeat(x.shape[0], ids_restore.shape[1] + 1 - x.shape[1], 1)
        x_ = torch.cat([x[:, 1:, :], mask_tokens], dim=1)  # no cls token
        x_ = torch.gather(x_, dim=1, index=ids_restore.unsqueeze(-1).repeat(1, 1, x.shape[2]))  # unshuffle
        x = torch.cat([x[:, :1, :], x_], dim=1)  # append cls token

        # add pos embed
        x = x + self.decoder_pos_embed

        # apply Transformer blocks
        x_mid = self.decoder_blocks[0](x)
        x = x_mid
        for blk in self.decoder_blocks[1:]:
            x = blk(x)
        x = self.decoder_norm(x)

        # predictor projection
        x = self.decoder_pred(x)

        # remove cls token
        x = x[:, 1:, :]

        return x, x_mid


class Encoder(nn.Module):
    def __init__(self, img_size, patch_size, in_chans, embed_dim, depth, num_heads, mlp_ratio, norm_layer):
        super().__init__()
        self.patch_embed = PatchEmbed(img_size, patch_size, in_chans, embed_dim)
        num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim),
                                      requires_grad=False)  # fixed sin-cos embedding

        self.blocks = nn.ModuleList([
            Block(embed_dim, num_heads, mlp_ratio, qkv_bias=True, qk_scale=None, norm_layer=norm_layer)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)

    def forward(self, x, mask_ratio):
        # embed patches
        x = self.patch_embed(x)

        # add pos embed w/o cls token
        x = x + self.pos_embed[:, 1:, :]

        # masking: length -> length * mask_ratio
        x, mask, ids_restore = self.random_masking(x, mask_ratio)

        # append cls token
        cls_token = self.cls_token + self.pos_embed[:, :1, :]
        cls_tokens = cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)

        # apply Transformer blocks
        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)

        return x, mask, ids_restore

    def random_masking(self, x, mask_ratio):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - mask_ratio))

        noise = torch.rand(N, L, device=x.device)  # noise in [0, 1]

        # sort noise for each sample
        ids_shuffle = torch.argsort(noise, dim=1)  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(ids_shuffle, dim=1)

        # keep the first subset
        ids_keep = ids_shuffle[:, :len_keep]
        x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)

        return x_masked, mask, ids_restore


class CycleMAE(nn.Module):
    """ Masked Autoencoder with VisionTransformer backbone
    """

    def __init__(self, num_domains=3, img_size=224, patch_size=16, in_chans=3,
                 embed_dim=1024, depth=24, num_heads=16,
                 decoder_embed_dim=1024, decoder_depth=8, decoder_num_heads=16,
                 mlp_ratio=4., norm_layer=nn.LayerNorm, norm_pix_loss=False):
        super().__init__()

        self.num_domains = num_domains
        self.encoder = Encoder(img_size, patch_size, in_chans, embed_dim, depth, num_heads, mlp_ratio, norm_layer)
        # --------------------------------------------------------------------------
        # MAE encoder specifics
        # self.patch_embed = PatchEmbed(img_size, patch_size, in_chans, embed_dim)
        num_patches = self.encoder.patch_embed.num_patches

        # self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        # fixed sin-cos embedding
        # self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim), requires_grad=False)

        # self.blocks = nn.ModuleList([
        #     Block(embed_dim, num_heads, mlp_ratio, qkv_bias=True, qk_scale=None, norm_layer=norm_layer)
        #     for i in range(depth)])
        # self.norm = norm_layer(embed_dim)

        # -----------Decoder------------
        multi_domain_decoders_dict = {}

        for i in range(num_domains):
            multi_domain_decoders_dict[f'decoder_{i}'] = Decoder(
                embed_dim,
                patch_size,
                num_patches,
                in_chans=in_chans,
                decoder_embed_dim=decoder_embed_dim,
                decoder_depth=decoder_depth,
                decoder_num_heads=decoder_num_heads,
                mlp_ratio=mlp_ratio,
                norm_layer=nn.LayerNorm,
                norm_pix_loss=False
            )
        self.multi_domain_decoders = nn.ModuleDict(multi_domain_decoders_dict)
        print('------')

        # self.add_module(f'decoder_{i}', Decoder(
        #                             embed_dim,
        #                             patch_size,
        #                             num_patches,
        #                             in_chans=in_chans,
        #                             decoder_embed_dim=decoder_embed_dim,
        #                             decoder_depth=decoder_depth,
        #                             decoder_num_heads=decoder_num_heads,
        #                             mlp_ratio=mlp_ratio,
        #                             norm_layer=nn.LayerNorm,
        #                             norm_pix_loss=False
        #                             ))

        # --------------------------------------------------------------------------

        self.norm_pix_loss = norm_pix_loss

        self.initialize_weights()

    def initialize_weights(self):
        d = torch.load('./mae_pretrain_vit_large.pth')
        self.encoder.load_state_dict(d['model'])
        # initialization
        # initialize (and freeze) pos_embed by sin-cos embedding
        # pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1], int(self.patch_embed.num_patches**.5), cls_token=True)
        # self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

        # # decoder_pos_embed = get_2d_sincos_pos_embed(self.decoder_pos_embed.shape[-1], int(self.patch_embed.num_patches**.5), cls_token=True)

        # # initialize patch_embed like nn.Linear (instead of nn.Conv2d)
        # w = self.patch_embed.proj.weight.data
        # torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

        # # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
        # torch.nn.init.normal_(self.cls_token, std=.02)

        # # initialize nn.Linear and nn.LayerNorm
        # self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

            # def random_masking(self, x, mask_ratio):

    #     """
    #     Perform per-sample random masking by per-sample shuffling.
    #     Per-sample shuffling is done by argsort random noise.
    #     x: [N, L, D], sequence
    #     """
    #     N, L, D = x.shape  # batch, length, dim
    #     len_keep = int(L * (1 - mask_ratio))

    #     noise = torch.rand(N, L, device=x.device)  # noise in [0, 1]

    #     # sort noise for each sample
    #     ids_shuffle = torch.argsort(noise, dim=1)  # ascend: small is keep, large is remove
    #     ids_restore = torch.argsort(ids_shuffle, dim=1)

    #     # keep the first subset
    #     ids_keep = ids_shuffle[:, :len_keep]
    #     x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))

    #     # generate the binary mask: 0 is keep, 1 is remove
    #     mask = torch.ones([N, L], device=x.device)
    #     mask[:, :len_keep] = 0
    #     # unshuffle to get the binary mask
    #     mask = torch.gather(mask, dim=1, index=ids_restore)

    #     return x_masked, mask, ids_restore

    def patchify(self, imgs):
        """
        imgs: (N, 3, H, W)
        x: (N, L, patch_size**2 *3)
        """
        p = self.encoder.patch_embed.patch_size[0]
        assert imgs.shape[2] == imgs.shape[3] and imgs.shape[2] % p == 0

        h = w = imgs.shape[2] // p
        x = imgs.reshape(shape=(imgs.shape[0], 3, h, p, w, p))
        x = torch.einsum('nchpwq->nhwpqc', x)
        x = x.reshape(shape=(imgs.shape[0], h * w, p ** 2 * 3))
        return x

    def unpatchify(self, x):
        """
        x: (N, L, patch_size**2 *3)
        imgs: (N, 3, H, W)
        """
        p = self.encoder.patch_embed.patch_size[0]
        h = w = int(x.shape[1] ** .5)
        assert h * w == x.shape[1]

        x = x.reshape(shape=(x.shape[0], h, w, p, p, 3))
        x = torch.einsum('nhwpqc->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], 3, h * p, h * p))
        return imgs

    def forward_loss(self, imgs, pred, mask):
        """
        imgs: [N, 3, H, W]
        pred: [N, L, p*p*3]
        mask: [N, L], 0 is keep, 1 is remove,
        """
        target = self.patchify(imgs)
        if self.norm_pix_loss:
            mean = target.mean(dim=-1, keepdim=True)
            var = target.var(dim=-1, keepdim=True)
            target = (target - mean) / (var + 1.e-6) ** .5

        loss = (pred - target) ** 2
        loss = loss.mean(dim=-1)  # [N, L], mean loss per patch
        return loss
        # loss = (loss * mask).sum() / mask.sum()  # mean loss on removed patches
        # return loss

    # def forward_encoder(self, x, mask_ratio):
    # # embed patches
    # x = self.patch_embed(x)

    # # add pos embed w/o cls token
    # x = x + self.pos_embed[:, 1:, :]

    # # masking: length -> length * mask_ratio
    # x, mask, ids_restore = self.random_masking(x, mask_ratio)

    # # append cls token
    # cls_token = self.cls_token + self.pos_embed[:, :1, :]
    # cls_tokens = cls_token.expand(x.shape[0], -1, -1)
    # x = torch.cat((cls_tokens, x), dim=1)

    # # apply Transformer blocks
    # for blk in self.blocks:
    #     x = blk(x)
    # x = self.norm(x)

    # return x, mask, ids_restore

    def forward(self, x, original_x, mask_ratio=0.75):
        # latent, mask, ids_restore = self.forward_encoder(x, mask_ratio)
        latent, mask, ids_restore = self.encoder(x, mask_ratio)
        loss_recons_list = []
        pred_list = []

        for i in range(3):
            pred, _ = self.multi_domain_decoders[f'decoder_{i}'](latent, ids_restore)
            # loss = self.forward_loss(x, pred, mask)
            loss_recons = self.forward_loss(original_x, pred, mask)
            pred_list.append(pred)
            loss_recons_list.append(loss_recons)

        # 计算重建loss(标量)

        bz = int(loss_recons_list[0].shape[0] / 3)
        loss_recons_list_useful = []
        for i in range(3):
            loss_recons_list_useful.append(loss_recons_list[i][i * bz:(i + 1) * bz])

        loss_recons = (torch.cat(loss_recons_list_useful) * mask).sum() / mask.sum()

        # 计算cycle loss
        # cycle_pred_list = []
        loss_cycle = 0

        decoder_mid_feature_list = [[] for _ in range(3)]
        for i in range(3):
            loss_cycle_list = []
            # latent, mask, ids_restore = self.forward_encoder(self.unpatchify(pred_list[i].detach()), mask_ratio)
            latent, mask, ids_restore = self.encoder(self.unpatchify(pred_list[i].detach()), mask_ratio)

            for j in range(3):
                pred, decoder_mid_feature = self.multi_domain_decoders[f'decoder_{j}'](latent, ids_restore)
                loss_cycle_tmp = self.forward_loss(original_x[j * bz:(j + 1) * bz], pred[j * bz:(j + 1) * bz],
                                                   mask[j * bz:(j + 1) * bz])
                loss_cycle_list.append(loss_cycle_tmp)
                decoder_mid_feature_list[j].append(decoder_mid_feature)

            loss_cycle += (torch.cat(loss_cycle_list) * mask).sum() / mask.sum()

        # 计算contrastive loss
        tao = 100000
        loss_contrastive = 0
        for i in range(3):
            decoder_mid_feature = torch.stack(decoder_mid_feature_list[i])
            new_shape = torch.Size([3, 3, -1]) + decoder_mid_feature.shape[-2:]
            decoder_mid_feature = decoder_mid_feature.reshape(new_shape)
            decoder_mid_feature = decoder_mid_feature.permute(1, 0, 2, 3, 4)
            decoder_mid_feature = decoder_mid_feature.flatten(1, 2)

            loss_contrastive += (-1) * torch.nn.functional.log_softmax(
                (decoder_mid_feature[i] * decoder_mid_feature).reshape(3, -1).sum(1) / tao, dim=0)[i]

        loss = loss_recons + 2 * loss_cycle + 2 * loss_contrastive
        return loss
