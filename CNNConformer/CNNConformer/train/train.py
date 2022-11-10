import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import os
from pathlib import Path
from tqdm import tqdm
import torch
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.multiprocessing as multiprocessing
from torch.utils.tensorboard import SummaryWriter

from ..models import Conformer
from ..tools.get_num_class import get_num_class
from ..tools.decoder import CTCDecoder
from .dataset import SpeLabDataset
from .schedule import CosExp
from .cer import CER

from CausalHiFiGAN.tools.file_io import load_json, load_list
from CausalHiFiGAN.tools.wav_io import load_wav

torch.backends.cudnn.benchmark = True


def train(path_dir_list=Path("./data/list"),
          path_dir_param=Path("./data/param"),
          path_dir_checkpoint=Path("./checkpoint"),
          path_dir_log=Path("./log"),
          path_config="./config.json",
          path_config_vocoder="../CausalHiFiGAN/config_v1.json"):
    print("--- train ---")

    # prepare directory

    path_dir_checkpoint.mkdir(exist_ok=1)
    path_dir_log.mkdir(exist_ok=1)

    # load config

    h = load_json(path_config)
    hv = load_json(path_config_vocoder)

    # prepare device

    if multiprocessing.get_start_method() == 'fork' and h.num_workers != 0:
        multiprocessing.set_start_method('spawn', force=True)

    torch.manual_seed(h.seed)
    torch.cuda.manual_seed(h.seed)
    device = torch.device(h.device)

    # prepare model

    num_class = get_num_class(path_dir_param / "phoneme.json")
    recognizer = Conformer(num_class).to(device)

    # prepare dataset

    dataset_train = SpeLabDataset(path_dir_list / "wav_train.txt",
                                  path_dir_list / "lab_train.txt",
                                  path_dir_param / "stats.json",
                                  path_dir_param / "phoneme.json",
                                  h, hv,
                                  h.segment_size,
                                  randomize=True)
    dataset_valid = SpeLabDataset(path_dir_list / "wav_valid.txt",
                                  path_dir_list / "lab_valid.txt",
                                  path_dir_param / "stats.json",
                                  path_dir_param / "phoneme.json",
                                  h, hv,
                                  h.segment_size_valid,
                                  randomize=False)

    dataloader_train = DataLoader(dataset_train,
                                  h.batch_size,
                                  shuffle=True,
                                  drop_last=True,
                                  num_workers=h.num_workers,
                                  pin_memory=True)
    dataloader_valid = DataLoader(dataset_valid,
                                  h.batch_size_valid,
                                  shuffle=False,
                                  drop_last=False,
                                  num_workers=h.num_workers,
                                  pin_memory=True)

    # prepare optimizer

    optimizer = torch.optim.RAdam(recognizer.parameters(),
                                  h.lr,
                                  h.betas,
                                  h.eps,
                                  h.weight_decay)

    cosexp = CosExp(h.epoch_warmup * len(dataloader_train),
                    h.epoch_switch * len(dataloader_train),
                    h.weight_lr_initial,
                    h.weight_lr_final)
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, cosexp)

    path_cp = path_dir_checkpoint / "state_latest.cp"
    if path_cp.exists():
        cp = torch.load(path_cp, map_location=lambda storage, loc: storage)
        recognizer.load_state_dict(cp["recognizer"])
        optimizer.load_state_dict(cp["optimizer"])
        scheduler.load_state_dict(cp["scheduler"])
        epoch = cp["epoch"]
        step = cp["step"]
        best_cer = cp["best_cer"]
        del cp
        print(f"loaded {path_cp}")
    else:
        epoch = 0
        step = 0
        best_cer = 1.0

    # prepare loss function

    f_cer = CER(blank=0)
    decoder = CTCDecoder(path_dir_param / "phoneme.json")

    # prepare tensorboard

    sw = SummaryWriter(path_dir_log)

    list_path_sample = load_list(path_dir_list / "valid_sample.txt")
    list_index_sample = [dataset_valid.list_path_wav.index(path_sample) for path_sample in list_path_sample]
    list_index_sample = [(i // h.batch_size_valid, i % h.batch_size_valid) for i in list_index_sample]
    count_sample = 0
    len_list_sample = len(list_index_sample)

    # start training

    while(epoch < h.epochs):
        epoch += 1
        print(f"--- epoch {epoch} train ---")

        # train

        for batch in tqdm(dataloader_train):
            step += 1

            # forward

            spe, lab = [item.to(device) for item in batch]

            p_lab_h = recognizer(spe)

            loss = F.cross_entropy(p_lab_h.transpose(-1, -2), lab)

            # backward

            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()

            # write log

            sw.add_scalar("train/lr", scheduler.get_last_lr()[0], step)
            scheduler.step()

            sw.add_scalar("train/loss", loss, step)

        # valid

        if epoch % h.valid_interval == 0:
            print(f"--- epoch {epoch} valid ---")

            recognizer.eval()
            with torch.no_grad():
                loss = 0.0
                cer = 0.0
                i = 0
                for j, batch in tqdm(enumerate(dataloader_valid)):
                    # forward

                    spe, lab = [item.to(device) for item in batch]

                    p_lab_h = recognizer(spe)

                    loss += F.cross_entropy(p_lab_h.transpose(-1, -2), lab)
                    cer += f_cer(p_lab_h.to("cpu"), lab.to("cpu"))

                    # visualize samples

                    if i < len_list_sample:
                        if j == list_index_sample[i][0]:
                            name_sample = f"{list_path_sample[i].parent.stem}_{list_path_sample[i].stem}"
                            if count_sample < len_list_sample:
                                audio = torch.from_numpy(load_wav(list_path_sample[i]))
                                sw.add_audio('target/wav_{}'.format(name_sample),
                                             audio.unsqueeze(0), 0, hv.sampling_rate)
                                count_sample += 1

                            text = decoder(p_lab_h[list_index_sample[i][1]])
                            sw.add_text('generated/sentence_{}'.format(name_sample),
                                        text, step)
                            i += 1

                loss /= len(dataloader_valid)
                cer /= len(dataloader_valid)

                # write log

                sw.add_scalar("valid/loss", loss, step)
                sw.add_scalar("valid/CER", cer, step)

                # save state

                torch.save(
                    {"recognizer": recognizer.state_dict(),
                     "optimizer": optimizer.state_dict(),
                     "scheduler": scheduler.state_dict(),
                     "epoch": epoch,
                     "step": step,
                     "best_cer": best_cer},
                    path_dir_checkpoint / "state_latest.cp")

                print("saved state_latest.cp")

                if cer <= best_cer:
                    best_cer = cer
                    torch.save(
                        recognizer.state_dict(),
                        path_dir_checkpoint / "recognizer_best.cp")
                    print("saved recognizer_best.cp")

            recognizer.train()
