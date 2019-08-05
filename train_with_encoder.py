import argparse
import random
import math
import os, re
from collections import OrderedDict

from tqdm import tqdm
import numpy as np
from PIL import Image

import torch
from torch import nn, optim
from torch.nn import functional as F
from torch.autograd import Variable, grad
from torch.utils.data import DataLoader
from torchvision import datasets, transforms, utils

from dataset import MultiResolutionDataset
from model import StyledGeneratorWithEncoder, Discriminator


def requires_grad(model, flag=True):
    for p in model.parameters():
        p.requires_grad = flag


def dataPar_state_dict(state_dict):
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        if 'module' not in k:
            k = 'module.' + k
        else:
            k = k.replace('features.module.', 'module.features.')
        new_state_dict[k] = v
    return new_state_dict


def accumulate(model1, model2, decay=0.999):
    par1 = dict(model1.named_parameters())
    par2 = dict(model2.named_parameters())

    for k in par1.keys():
        par1[k].data.mul_(decay).add_(1 - decay, par2[k].data)


def sample_data(dataset, batch_size, image_size=4):
    dataset.resolution = image_size
    loader = DataLoader(dataset, shuffle=True, batch_size=batch_size, num_workers=16)

    return loader


def adjust_lr(optimizer, lr):
    for group in optimizer.param_groups:
        mult = group.get('mult', 1)
        group['lr'] = lr * mult


def train(args, dataset, generator, discriminator, step=None):
    if step is None:
        step = int(math.log2(args.init_size)) - 2

    resolution = 4 * 2 ** step
    loader = sample_data(
        dataset, args.batch.get(resolution, args.batch_default), resolution
    )
    data_loader = iter(loader)

    adjust_lr(g_optimizer, args.lr.get(resolution, 0.001))
    adjust_lr(d_optimizer, args.lr.get(resolution, 0.001))

    pbar = tqdm(range(3_000_000))

    requires_grad(generator, False)
    requires_grad(discriminator, True)

    disc_loss_val = 0
    gen_loss_val = 0
    grad_loss_val = 0

    alpha = 0
    used_sample = 0

    max_step = int(math.log2(args.max_size)) - 2
    final_progress = False

    reconstruction_loss = nn.MSELoss()

    for i in pbar:
        discriminator.zero_grad()

        alpha = min(1, 1 / args.phase * (used_sample + 1))

        if resolution == args.init_size or final_progress:
            alpha = 1

        if used_sample > args.phase * 2:
            used_sample = 0
            step += 1
            if step > 4:
                step = 4

            if step > max_step:
                step = max_step
                final_progress = True

            else:
                alpha = 0

            resolution = 4 * 2 ** step

            loader = sample_data(
                dataset, args.batch.get(resolution, args.batch_default), resolution
            )
            data_loader = iter(loader)

            torch.save(
                {
                    'generator': generator.module.state_dict(),
                    'discriminator': discriminator.module.state_dict(),
                    'g_optimizer': g_optimizer.state_dict(),
                    'd_optimizer': d_optimizer.state_dict(),
                    'g_running': g_running.state_dict()
                },
                f'checkpoint/train_step-{step}.model',
            )

            adjust_lr(g_optimizer, args.lr.get(resolution, 0.001))
            adjust_lr(d_optimizer, args.lr.get(resolution, 0.001))

        try:
            real_image = next(data_loader)

        except (OSError, StopIteration):
            data_loader = iter(loader)
            real_image = next(data_loader)

        used_sample += real_image.shape[0]

        b_size = real_image.size(0)
        real_image = real_image.cuda()

        if args.loss == 'wgan-gp':
            real_predict = discriminator(real_image, step=step, alpha=alpha)
            real_predict = real_predict.mean() - 0.001 * (real_predict ** 2).mean()
            (-real_predict).backward()

        elif args.loss == 'r1':
            real_image.requires_grad = True
            real_predict = discriminator(real_image, step=step, alpha=alpha)
            real_predict = F.softplus(-real_predict).mean()
            real_predict.backward(retain_graph=True)

            grad_real = grad(
                outputs=real_predict.sum(), inputs=real_image, create_graph=True
            )[0]
            grad_penalty = (
                    grad_real.view(grad_real.size(0), -1).norm(2, dim=1) ** 2
            ).mean()
            grad_penalty = 10 / 2 * grad_penalty
            grad_penalty.backward()
            grad_loss_val = grad_penalty.item()

        try:
            real_image_gen = next(data_loader)

        except (OSError, StopIteration):
            data_loader = iter(loader)
            real_image_gen = next(data_loader)

        real_image_gen = real_image_gen.cuda()
        fake_image, _ = generator(real_image_gen, step=step, alpha=alpha)
        fake_predict = discriminator(fake_image, step=step, alpha=alpha)

        if args.loss == 'wgan-gp':
            fake_predict = fake_predict.mean()
            fake_predict.backward()

            eps = torch.rand(b_size, 1, 1, 1).cuda()
            x_hat = eps * real_image.data + (1 - eps) * fake_image.data
            x_hat.requires_grad = True
            hat_predict = discriminator(x_hat, step=step, alpha=alpha)
            grad_x_hat = grad(
                outputs=hat_predict.sum(), inputs=x_hat, create_graph=True
            )[0]
            grad_penalty = (
                    (grad_x_hat.view(grad_x_hat.size(0), -1).norm(2, dim=1) - 1) ** 2
            ).mean()
            grad_penalty = 10 * grad_penalty
            grad_penalty.backward()
            grad_loss_val = grad_penalty.item()
            disc_loss_val = (real_predict - fake_predict).item()

        elif args.loss == 'r1':
            fake_predict = F.softplus(fake_predict).mean()
            fake_predict.backward()
            disc_loss_val = (real_predict + fake_predict).item()

        d_optimizer.step()

        if (i + 1) % n_critic == 0:
            generator.zero_grad()

            requires_grad(generator, True)
            requires_grad(discriminator, False)

            try:
                real_image_gen = next(data_loader)

            except (OSError, StopIteration):
                data_loader = iter(loader)
                real_image_gen = next(data_loader)

            real_image_gen = real_image_gen.cuda()
            fake_image, style = generator(real_image_gen, step=step, alpha=alpha)

            predict = discriminator(fake_image, step=step, alpha=alpha)

            if args.loss == 'wgan-gp':
                loss = -predict.mean()

            elif args.loss == 'r1':
                loss = args.lambda_1 * F.softplus(-predict).mean() + args.lambda_2 * reconstruction_loss(fake_image, real_image_gen)
                if args.lambda_3 > 0.0:
                    loss += args.lambda_3 * reconstruction_loss(generator.encoder(fake_image), style)

            gen_loss_val = loss.item()

            loss.backward()
            g_optimizer.step()
            accumulate(g_running, generator.module)

            requires_grad(generator, False)
            requires_grad(discriminator, True)

        if (i + 1) % 100 == 0:
            images = []

            with torch.no_grad():
                try:
                    real_image_gen = next(data_loader)
                except (OSError, StopIteration):
                    data_loader = iter(loader)
                    real_image_gen = next(data_loader)
                real_image_gen = real_image_gen.cuda()

                images.append(
                    g_running(
                        real_image_gen[:50], step=step, alpha=alpha
                    )[0].data.cpu()
                )

            utils.save_image(
                torch.cat(images, 0),
                args.save_path + f'sample/{str(i + 1).zfill(6)}.png',
                nrow=10,
                normalize=True,
                range=(-1, 1),
            )

        if (i + 1) % 10000 == 0:
            torch.save(
                g_running.state_dict(), args.save_path + f'checkpoint/{str(i + 1).zfill(6)}.model'
            )

        state_msg = (
            f'Size: {4 * 2 ** step}; G: {gen_loss_val:.3f}; D: {disc_loss_val:.3f};'
            f' Grad: {grad_loss_val:.3f}; Alpha: {alpha:.5f}'
        )

        pbar.set_description(state_msg)


if __name__ == '__main__':
    code_size = 512
    batch_size = 16
    n_critic = 1
    step = None

    parser = argparse.ArgumentParser(description='Progressive Growing of GANs')
    parser.add_argument('path', type=str, help='path of specified dataset')
    parser.add_argument('--phase', type=int, default=600_000, help='number of samples used for each training phases')
    parser.add_argument('--lr', default=0.001, type=float, help='learning rate')
    parser.add_argument('--sched', action='store_true', help='use lr scheduling')
    parser.add_argument('--init_size', default=8, type=int, help='initial image size')
    parser.add_argument('--max_size', default=1024, type=int, help='max image size')
    parser.add_argument('--mixing', action='store_true', help='use mixing regularization')
    parser.add_argument('--loss', type=str, default='wgan-gp', choices=['wgan-gp', 'r1'], help='class of gan loss')
    parser.add_argument('--save_path', type=str, default='', help='path to saving dir.')
    parser.add_argument('--ckpt_path', type=str, default=None, help='path to pretrained model file.')
    parser.add_argument('--lambda_1', type=float, default=1.0, help='Strength of adversarial loss for the generator.')
    parser.add_argument('--lambda_2', type=float, default=1.0, help='Strength of content loss for generator.')
    parser.add_argument('--lambda_3', type=float, default=0.0, help='Strength of style loss on w.')
    args = parser.parse_args()

    generator = nn.DataParallel(StyledGeneratorWithEncoder(code_size)).cuda()
    discriminator = nn.DataParallel(Discriminator()).cuda()
    g_running = StyledGeneratorWithEncoder(code_size).cuda()
    g_running.train(False)

    class_loss = nn.CrossEntropyLoss()

    g_optimizer = optim.Adam(generator.module.generator.parameters(), lr=args.lr, betas=(0.0, 0.99))
    g_optimizer.add_param_group({'params': generator.module.encoder.progression.parameters(), 'lr': args.lr * 0.01,'mult': 0.01})
    g_optimizer.add_param_group({'params': generator.module.encoder.from_rgb.parameters(), 'lr': args.lr * 0.01, 'mult': 0.01})
    d_optimizer = optim.Adam(discriminator.parameters(), lr=args.lr, betas=(0.0, 0.99))

    if args.ckpt_path is not None:
        basename = os.path.basename(args.ckpt_path)
        regex = re.compile(r'\d+')
        numbers = [int(x) for x in regex.findall(basename)]
        step = numbers[-1]

        state_dict = torch.load(args.ckpt_path)
        generator.load_state_dict(dataPar_state_dict(state_dict['generator']))
        discriminator.load_state_dict(dataPar_state_dict(state_dict['discriminator']))
        g_optimizer.load_state_dict(state_dict['g_optimizer'])
        d_optimizer.load_state_dict(state_dict['d_optimizer'])
        g_running.load_state_dict(state_dict['g_running'])

    accumulate(g_running, generator.module, 0)

    transform = transforms.Compose(
        [
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]
    )

    dataset = MultiResolutionDataset(args.path, transform)

    if args.sched:
        args.lr = {128: 0.0015, 256: 0.002, 512: 0.003, 1024: 0.003}
        args.batch = {4: 512, 8: 256, 16: 128, 32: 64, 64: 32, 128: 32, 256: 32}

    else:
        args.lr = {}
        args.batch = {}

    args.gen_sample = {512: (8, 4), 1024: (4, 2)}

    args.batch_default = 32

    train(args, dataset, generator, discriminator, step)
