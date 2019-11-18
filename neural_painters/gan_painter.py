import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch import autograd
from torch.utils.tensorboard import SummaryWriter

from neural_painters.data import FullActionStrokeDataLoader


class Discriminator(nn.Module):
  def __init__(self, action_size, dim=16):
    super(Discriminator, self).__init__()
    self.dim = dim

    self.fc1 = nn.Linear(action_size, dim)
    self.conv1 = nn.Conv2d(3, dim, 4, stride=2, padding=1)
    self.conv2 = nn.Conv2d(dim, dim*2, 4, stride=2, padding=1)
    self.bn2 = nn.BatchNorm2d(dim*2)
    self.conv3 = nn.Conv2d(dim*2, dim*4, 4, stride=2, padding=1)
    self.bn3 = nn.BatchNorm2d(dim*4)
    self.conv4 = nn.Conv2d(dim*4, dim*8, 4, stride=2, padding=1)
    self.bn4 = nn.BatchNorm2d(dim*8)
    self.fc2 = nn.Linear(4*4*(dim*8), 1)
    self.leaky_relu = nn.LeakyReLU(negative_slope=0.2)

  def forward(self, images, actions):
    actions = F.relu(self.fc1(actions))
    actions = actions.view(-1, self.dim, 1, 1)
    x = self.leaky_relu(self.conv1(images))

    x = x + actions
    x = self.leaky_relu(self.bn2(self.conv2(x)))
    x = self.leaky_relu(self.bn3(self.conv3(x)))
    x = self.leaky_relu(self.bn4(self.conv4(x)))
    x = x.flatten(start_dim=1)
    x = self.fc2(x)
    return x


class Generator(nn.Module):
  def __init__(self, action_size, dim=16):
    super(Generator, self).__init__()
    self.dim = dim

    self.fc1 = nn.Linear(action_size, 4*4*(dim*16))  # This seems.. wrong.  Should it be dim*8?
    self.bn1 = nn.BatchNorm2d(dim*16)
    self.deconv1 = nn.ConvTranspose2d(dim*16, dim*8, 4, stride=2, padding=1)
    self.bn2 = nn.BatchNorm2d(dim*8)
    self.deconv2 = nn.ConvTranspose2d(dim*8, dim*4, 4, stride=2, padding=1)
    self.bn3 = nn.BatchNorm2d(dim*4)
    self.deconv3 = nn.ConvTranspose2d(dim*4, dim*2, 4, stride=2, padding=1)
    self.bn4 = nn.BatchNorm2d(dim*2)
    self.deconv4 = nn.ConvTranspose2d(dim*2, 3, 4, stride=2, padding=1)
    self.leaky_relu = nn.LeakyReLU(negative_slope=0.2)

  def forward(self, actions):
    # TODO: Add random noise

    x = self.fc1(actions)
    x = x.view(-1, self.dim*16, 4, 4)
    x = F.relu(self.bn1(x))
    x = F.relu(self.bn2(self.deconv1(x)))
    x = F.relu(self.bn3(self.deconv2(x)))
    x = F.relu(self.bn4(self.deconv3(x)))
    x = F.sigmoid(self.deconv4(x))
    return x.view(-1, 3, 64, 64)


def calc_gradient_penalty(discriminator: nn.Module, real_data: torch.Tensor,
                          fake_data: torch.Tensor, actions: torch.Tensor,
                          device: torch.device, scale: float):
  batch_size = real_data.shape[0]
  epsilon = torch.rand(batch_size, 1)  # in my tf implementation, same epsilon used for all samples in minibatch
  epsilon = epsilon.expand(batch_size, real_data.nelement()//batch_size).contiguous().view(batch_size, 3, 64, 64)
  epsilon = epsilon.to(device)

  interpolates = epsilon * real_data + ((1.0 - epsilon) * fake_data)
  interpolates.requires_grad = True

  disc_interpolates = discriminator(interpolates, actions)
  gradients = autograd.grad(disc_interpolates, interpolates,
                            grad_outputs=torch.ones_like(disc_interpolates),
                            create_graph=True)[0]
  gradients = gradients.view(batch_size, -1)

  gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean() * scale

  return gradient_penalty


def train_gan_neural_painter(action_size: int,
                             dim_size: int,
                             batch_size: int,
                             device: torch.device,
                             data_dir: str,
                             disc_iters: int = 5,
                             save_every_n_steps: int = 25000,
                             log_every_n_steps: int = 2000,
                             tensorboard_every_n_steps: int = 100,
                             tensorboard_log_dir: str = 'logdir',
                             save_dir: str = 'gan_train_checkpoints',
                             save_name: str = 'gan_neural_painter'):
  # Initialize data loader
  loader = FullActionStrokeDataLoader(data_dir, batch_size, False)

  # Initialize networks and optimizers
  discriminator = Discriminator(action_size, dim=dim_size).to(device).train()
  generator = Generator(action_size, dim=dim_size).to(device).train()

  optim_disc = optim.Adam(discriminator.parameters(), lr=1e-4)
  optim_gen = optim.Adam(generator.parameters(), lr=1e-4)

  batch_idx_offset = 0
  # Initialize tensorboard a.k.a. greatest thing since sliced bread
  writer = SummaryWriter(tensorboard_log_dir)
  for batch_idx, batch in enumerate(loader):
    batch_idx += batch_idx_offset

    strokes = batch['stroke'].float().to(device)
    actions = batch['action'].float().to(device)

    if (batch_idx + 1) % (disc_iters + 1) == 0:  # Generator step every disc_iters+1 steps
      for p in discriminator.parameters():
        p.requires_grad = False  # to avoid computation (i copied this code, but this makes no sense i think?)
      optim_gen.zero_grad()

      generated = generator(actions)
      generated_score = torch.mean(discriminator(generated, actions))

      generator_loss = generated_score
      generator_loss.backward()
      optim_gen.step()

      writer.add_scalar('generator_loss', generator_loss, batch_idx)
    else:  # Discriminator steps for everything else
      for p in discriminator.parameters():
        p.requires_grad = True  # they are set to False in generator update
      optim_disc.zero_grad()

      real_score = torch.mean(discriminator(strokes, actions))

      generated = generator(actions)
      generated_score = torch.mean(discriminator(generated, actions))

      gradient_penalty = calc_gradient_penalty(discriminator, strokes.detach(),
                                               generated.detach(), actions,
                                               device, 10.0)

      disc_loss = real_score - generated_score + gradient_penalty
      disc_loss.backward()
      optim_disc.step()

      writer.add_scalar('discriminator_loss', disc_loss, batch_idx)
      writer.add_scalar('real_score', real_score, batch_idx)
      writer.add_scalar('generated_score', generated_score, batch_idx)
      writer.add_scalar('gradient_penalty', gradient_penalty, batch_idx)

    if batch_idx % tensorboard_every_n_steps == 0:
      writer.add_images('img_in', strokes[:3], batch_idx)
      writer.add_images('img_out', generated[:3], batch_idx)
    if batch_idx % log_every_n_steps == 0:
      print('train batch {}'.format(batch_idx))