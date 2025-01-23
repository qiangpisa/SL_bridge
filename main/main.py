import argparse
import torch
from tqdm import tqdm
# from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import torch.nn as nn
from torch.nn import functional as F
from models.gvae import GraphVAE
from torch.optim import Adam, SGD
from utils.data_loader import load_data
from utils.criterion import MMDLoss

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
from models.diffusion import DiffusionModel, AdvicedDiffusionModel, AdvicedDiffusionModel_yTmean
from utils.train_utils import save_model, draw_data_distribution, calculate_mean_distance
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
import warnings
import numpy as np
import matplotlib.pyplot as plt
import os
from models.utils import set_random_seed
import pdb

ep = 0
threshold = 0.5

# args = None
warnings.filterwarnings("ignore", categoryPYCHARM=FutureWarning)


def encode_data(gvae, g, args):
    # with torch.no_grad():
    # return_args = []
    # for arg in args:
    arg = args.clone()
    arg, mean, logvar = gvae.embed(g, arg)
    expect_log_prob = -torch.mean(torch.sum(logvar, dim=-1))
    recon = gvae.decode(g, arg)
    recon_copy = recon.clone()
    # check if recon_{ep}.jpg exists
    if not os.path.exists(f"recon_{ep}.jpg"):
        if ep % 5 == 0:
            draw_data_distribution(recon_copy.detach().cpu().numpy(), f"recon_{ep}.jpg")
    recon_loss = gvae.criterion(recon, args)
    # print("kl_loss: ", -0.5 * torch.sum(1 + logvar - mean**2 - logvar.exp())/mean.shape[0])
    return arg, recon, recon_loss, expect_log_prob


def decode_data(gvae, g, *args):
    with torch.no_grad():
        return_args = []
        for arg in args:
            arg = gvae.decode(g, arg)
            return_args.append(arg)

    return return_args


def train_e2e(model: AdvicedDiffusionModel_yTmean, optimizer_diff, data_loader, writer, epoch, scheduler_diff=None,
              advisors=None):
    model.train()
    loss_diff_this_epoch = 0
    for i, [data, advisor] in enumerate(zip(data_loader, advisors)):
        data = data.to(device)
        optimizer_diff.zero_grad()

        gt = data.ndata['label']
        if len(gt.shape) == 2:
            gt = gt.unsqueeze(0)
        assert len(gt.shape) == 3

        cond = data.ndata['feat']

        loss, loss_guide = model.train_step(gt, data, cond, advisor, advisor.unsqueeze(0))
        loss.backward()
        optimizer_diff.step()
        if scheduler_diff:
            scheduler_diff.step()

        loss_diff_this_epoch += loss.item()
    loss_diff_this_epoch = loss_diff_this_epoch / len(data_loader)
    writer.add_scalar('Train/Loss_diff', loss_diff_this_epoch, epoch)
    return loss_diff_this_epoch


def validate_in_train(model: AdvicedDiffusionModel_yTmean, valid_dataloader, writer, epoch, compare_dataloader=None,
                      advisors=None):
    model.eval()
    with torch.no_grad():
        loss_this_epoch = 0
        for i, [data, advisor] in enumerate(zip(valid_dataloader, advisors)):  # data:graph

            data = data.to(device)
            gt = data.ndata['label']
            if len(gt.shape) == 2:
                gt = gt.unsqueeze(0)
            assert len(gt.shape) == 3
            if i == 0:
                gts = gt

            else:
                gts = torch.cat((gts, gt), dim=0)

            gt_index = np.argwhere(data.ndata['label'].squeeze().cpu().numpy() > 0).squeeze(-1)
            # cond = encode_data(gvae, data, data.ndata['feat'])[0]
            cond = data.ndata['feat']

            loss, loss_guide = model.train_step(gt, data, cond, advisor, advisor.unsqueeze(0))
            loss_this_epoch += loss.item()

            mask = (data.ndata['feat'] > 0).squeeze().clone().float().unsqueeze(0)
            # cond = data.ndata['feat']
            if len(cond.shape) == 2:
                cond = cond.unsqueeze(0)
            assert len(cond.shape) == 3

            sample = model.sample(data, cond, advisor, advisor.unsqueeze(0))  # problem here?
            uncond_vec = (sample > threshold).float().squeeze().detach().unsqueeze(0)

            uncond_id = np.argwhere(uncond_vec.squeeze().cpu().numpy() > 0).squeeze(-1)
            uncond_topk = torch.topk(sample, k=len(gt_index), dim=1)[1]
            uncond_id = uncond_topk.squeeze().cpu().numpy()

            assert len(sample.shape) == 3
            if i == 0:
                samples = sample
                uncond_vecs = uncond_vec
            else:
                samples = torch.cat((samples, sample), dim=0)
                uncond_vecs = torch.cat((uncond_vecs, uncond_vec), dim=0)



        for i, data in enumerate(compare_dataloader):
            data = data.to(device)
            # gt = encode_data(gvae, data, data.ndata['label'])[0]
            gt_comp = data.ndata['label']
            if len(gt_comp.shape) == 2:
                gt_comp = gt_comp.unsqueeze(0)
            assert len(gt_comp.shape) == 3
            if i == 0:
                gts_comps = gt_comp
            else:
                gts_comps = torch.cat((gts_comps, gt_comp), dim=0)



        writer.add_scalar('Validation_intrain/Loss', loss_this_epoch, epoch)
        print(f"Epoch {epoch}, Validation loss {loss_this_epoch}")

    return loss_this_epoch


def train_gvae_diff(model, train_dataloader, valid_dataloader, test_dataloader, args, compare_dataloader=None,
                    advisors=None):
    save_path = args.save_path
    # create save path
    if not os.path.exists(save_path):
        os.makedirs(save_path)
    writer = SummaryWriter(
        f"./runs/{args.dataset}_{args.gnn_type}_{args.hidden_dim}_{args.num_layers}_{args.activation}_{args.mlp_layers}")
    torch.save(args,
               args.save_path + f"{args.dataset}_{args.gnn_type}_{args.hidden_dim}_{args.num_layers}_{args.activation}_{args.mlp_layers}.args")
    # optimizer_gvae = torch.optim.Adam(gvae.parameters(), lr=args_gvae.lr_vae/10)
    optimizer_diff = torch.optim.Adam(model.model.parameters(), lr=args.lr)
    # if args_gvae.scheduler:
    #     scheduler_gvae = torch.optim.lr_scheduler.StepLR(optimizer_gvae, step_size=200, gamma=0.97)
    # else:
    #     scheduler_gvae = None
    if args.scheduler:
        scheduler_diff = torch.optim.lr_scheduler.StepLR(optimizer_diff, step_size=200, gamma=0.97)
    else:
        scheduler_diff = None

    progress_bar = tqdm(range(args.max_epoch), desc='Training', dynamic_ncols=True)
    best_model = None
    for epoch in progress_bar:
        global ep
        ep = epoch
        if (epoch + 1) % 10 == 0:
            print("epoch: ", epoch)
        if (epoch) % 5 == 0:
            val_loss = validate_in_train(model, valid_dataloader, writer, epoch, compare_dataloader, advisors['valid'])
            # print(f"Epoch {epoch}, Validation loss {val_loss}")
            # print(f"Epoch {epoch}, Validation cond loss {val_cond_loss}")
            # print(f"Epoch {epoch}, Validation comp loss {comp_loss}")
        if (epoch) % 5 == 0:
            test(model, test_dataloader, epoch, advisors['test'])

        # save best model
        loss = train_e2e(model, optimizer_diff, train_dataloader, writer=writer, epoch=epoch,
                         scheduler_diff=scheduler_diff, advisors=advisors['train'])
        progress_bar.set_description(
            f"Epoch {epoch:.0f}, Train loss {loss:.4f}, lr_diff {optimizer_diff.param_groups[0]['lr']:.6f}")
        if epoch == 0:
            best_loss = val_loss
            best_epoch = epoch
            best_model = model.model.state_dict()
        else:
            if val_loss < best_loss:
                best_loss = val_loss
                best_epoch = epoch
                best_model = model.model.state_dict()
        if epoch % (args.max_epoch // 5) == 0:
            save_model(model,
                       save_path + f"{args.dataset}_{args.gnn_type}_{args.hidden_dim}_{args.num_layers}_{args.activation}_{args.mlp_layers}_at_{epoch}.pt")
    save_model(model,
               save_path + f"{args.dataset}_{args.gnn_type}_{args.hidden_dim}_{args.num_layers}_{args.activation}_{args.mlp_layers}_final.pt")
    # save best model
    torch.save(best_model,
               save_path + f"{args.dataset}_{args.gnn_type}_{args.hidden_dim}_{args.num_layers}_{args.activation}_{args.mlp_layers}_best_at_{best_epoch}.pt")
    print(f"Best model at epoch {best_epoch}, loss {best_loss}")
    writer.close()


import time


def test(model: AdvicedDiffusionModel_yTmean, test_dataloader, epoch=None, advisors=None):
    model = model.to(device)
    model.eval()
    with torch.no_grad():
        acc = 0
        f1 = 0
        precision = 0
        recall = 0
        time_start = time.time()
        for i, [data, advisor] in enumerate(zip(test_dataloader, advisors)):
            data = data.to(device)
            # gt = encode_data(gvae, data, data.ndata['label'])[0]
            gt = data.ndata['label']
            if len(gt.shape) == 2:
                gt = gt.unsqueeze(0)
            assert len(gt.shape) == 3
            gt = (gt.squeeze() > 0).int()

            # cond = encode_data(gvae, data, data.ndata['feat'])[0]
            cond = data.ndata['feat']
            if len(cond.shape) == 2:
                cond = cond.unsqueeze(0)
            assert len(cond.shape) == 3
            sample = model.sample(data, cond, advisor, advisor.unsqueeze(0))


            sample = sample.squeeze()


            sample = (sample > threshold).int()

            acc += accuracy_score(gt.cpu().numpy().flatten(), sample.cpu().numpy().flatten())
            f1 += f1_score(gt.cpu().numpy().flatten(), sample.cpu().numpy().flatten())
            precision += precision_score(gt.cpu().numpy().flatten(), sample.cpu().numpy().flatten())
            recall += recall_score(gt.cpu().numpy().flatten(), sample.cpu().numpy().flatten())
        time_end = time.time()
        time_per_sample = (time_end - time_start) / len(test_dataloader)
        acc = acc / len(test_dataloader)
        f1 = f1 / len(test_dataloader)
        precision = precision / len(test_dataloader)
        recall = recall / len(test_dataloader)
        print(
            f"Test accuracy {acc}, f1 {f1}, precision {precision}, recall {recall},  time per sample {time_per_sample}")
    return acc, f1, precision, recall


def train_cond(model, train_dataloader, valid_dataloader, args):
    optimizer_cond = torch.optim.Adam(model.model.conditional.parameters(), lr=args.lr)
    if args.scheduler:
        scheduler_cond = torch.optim.lr_scheduler.StepLR(optimizer_cond, step_size=400, gamma=0.99)
    else:
        scheduler_cond = None
    progress_bar = tqdm(range(args.max_cond_epoch), desc='Training', dynamic_ncols=True)
    for epoch in progress_bar:
        if epoch % 5 == 0:
            with torch.no_grad():
                valid_loss = 0
                for i, data in enumerate(valid_dataloader):
                    data = data.to(device)
                    cond = data.ndata['feat']
                    _, gt = model.lpsi(cond, data)
                    loss = model.train_conditioner(cond, data, gt)
                    valid_loss += loss.item()
                print(f"Epoch {epoch}, Validation loss {valid_loss / len(valid_dataloader)}")
        loss_cond_this_epoch = 0
        for i, data in enumerate(train_dataloader):
            data = data.to(device)
            optimizer_cond.zero_grad()

            cond = data.ndata['feat']
            _, gt = model.lpsi(cond, data)

            loss = model.train_conditioner(cond, data, gt)
            loss.backward()
            optimizer_cond.step()
            if scheduler_cond:
                scheduler_cond.step()

            loss_cond_this_epoch += loss.item()
        loss_cond_this_epoch = loss_cond_this_epoch / len(train_dataloader)
        progress_bar.set_description(
            f"Epoch {epoch:.0f}, Train loss {loss_cond_this_epoch:.4f}, lr_cond {optimizer_cond.param_groups[0]['lr']:.6f}")


def advisor_gen(model, train_dataloader, valid_dataloader, test_dataloader, eval_train_dataloader, args,
                advisors_names=['lpsi']):
    # advisor_train, advisor_valid, advisor_test, advisor_eval_train
    advisors = {}
    names = ["train", "valid", "test", "eval_train"]
    for i, dataloader in enumerate([train_dataloader, valid_dataloader, test_dataloader, eval_train_dataloader]):
        advisor_loader = []
        for j, data in enumerate(dataloader):
            data = data.to(device)
            cond = data.ndata['feat']
            seeds = []
            if 'lpsi' in advisors_names:
                seed, _ = model.lpsi(cond, data)
                seeds.append(seed)

            else:
                raise NotImplementedError

            seeds = torch.cat(seeds, dim=1)
            # seeds = seeds.squeeze().T
            advisor_loader.append(seeds)

        advisors[names[i]] = advisor_loader

    return advisors


def parse_args():
    parser = argparse.ArgumentParser(description='Diffusion')
    parser.add_argument("--dataset", type=str, default="jazz_IC50", help="dataset")
    parser.add_argument("--gnn_type", type=str, default="gcn", help="gnn type")
    parser.add_argument("--noise_emb_dim", type=int, default=128, help="noise embedding dimension")
    parser.add_argument("--hidden_dim", type=int, default=128, help="hidden dimension")
    parser.add_argument("--num_layers", type=int, default=5, help="number of hidden layers")
    parser.add_argument("--activation", type=str, default="prelu", help="activation function")
    parser.add_argument("--feat_drop", type=float, default=0.0, help="feature dropout")
    parser.add_argument("--attn_drop", type=float, default=0.0, help="attention dropout")
    parser.add_argument("--negative_slope", type=float, default=0.2, help="negative slope of leaky relu")
    parser.add_argument("--norm", type=str, default=None)
    parser.add_argument("--residual", type=bool, default=True)
    parser.add_argument("--scheduler", type=bool, default=True)
    parser.add_argument("--enc_nhead", type=int, default=4, help="number of heads in multi-head attention")
    parser.add_argument("--mlp_layers", type=int, default=4, help="number of layers in mlp")
    parser.add_argument("--lr", type=float, default=0.001, help="learning rate")
    parser.add_argument("--max_epoch", type=int, default=500, help="number of training epochs")
    # parser.add_argument("--batch_size", type=int, default=16, help="batch size")
    parser.add_argument("--save_path", type=str, default="./saved_diffusers/", help="save path")
    parser.add_argument("--num_advisors", type=int, default=1, help="number of advisors")
    parser.add_argument("--train_cond", type=bool, default=False, help="train the condition module")
    parser.add_argument("--max_cond_epoch", type=int, default=10, help="number of training epochs for condition module")
    parser.add_argument("--state_dict", type=str, default=None, help="state dict path")
    args = parser.parse_args()
    return args


if __name__ == "__main__":

    args = parse_args()
    print(args)

    train_dataloader, valid_dataloader, test_dataloader, eval_train_dataloader, num_features = load_data(args.dataset,
                                                                                                         dataset_path='datasets')
    print("len(train_dataloader): ", len(train_dataloader), "len(valid_dataloader): ", len(valid_dataloader),
          "len(test_dataloader): ", len(test_dataloader), "len(eval_train_dataloader): ", len(eval_train_dataloader))


    gnn_type = args.gnn_type
    in_dim = 1
    noise_emb_dim = args.noise_emb_dim
    hidden_dim = args.hidden_dim
    num_layers = args.num_layers
    activation = args.activation
    feat_drop = args.feat_drop
    attn_drop = args.attn_drop
    negative_slope = args.negative_slope
    residual = args.residual
    norm = args.norm
    enc_nhead = args.enc_nhead
    mlp_layers = args.mlp_layers
    num_advisors = args.num_advisors

    set_random_seed(44)
    model = AdvicedDiffusionModel_yTmean(gnn_type,
                                         in_dim,
                                         noise_emb_dim,
                                         hidden_dim,
                                         num_layers,
                                         activation,
                                         feat_drop,
                                         attn_drop,
                                         negative_slope,
                                         residual,
                                         norm,
                                         enc_nhead,
                                         mlp_layers,
                                         beta_schedule="linear",
                                         pred_x0=True,
                                         device=device,
                                         num_advisors=num_advisors)

    if args.state_dict:
        model.load_state_dict(torch.load(args.state_dict))
        # model = torch.load(args.state_dict, map_location=device)
    model = model.to(device)

    # gvae = gvae.to(device)
    cliped_eval_train_dataloader = []
    compare_dataloader = []
    eval_train_dataloader = list(eval_train_dataloader)
    used_rate = 0.25
    for i, items in enumerate(eval_train_dataloader):
        if (i >= int(used_rate * len(eval_train_dataloader))) and (
                i < int(used_rate * len(eval_train_dataloader)) + len(valid_dataloader)):
            compare_dataloader.append(items)
        elif i < int(used_rate * len(eval_train_dataloader)):
            cliped_eval_train_dataloader.append(items)
        else:
            break
    if args.train_cond:
        train_cond(model, train_dataloader, valid_dataloader, args)
    advisors = advisor_gen(model, train_dataloader, valid_dataloader, test_dataloader, cliped_eval_train_dataloader,
                           args)
    train_gvae_diff(model, train_dataloader, valid_dataloader, test_dataloader, args, compare_dataloader, advisors)
    test(model, test_dataloader, args.max_epoch + 1, advisors['test'])
