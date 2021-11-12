import os

import matplotlib.pyplot as plt
import neptune.new as neptune
import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn.functional as F
import yaml
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint
from pytorch_lightning.loggers.neptune import NeptuneLogger
from scikitplot.metrics import plot_confusion_matrix
from sklearn.metrics import accuracy_score
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader
from torch.utils.data import random_split
from torchvision import transforms
from torchvision.datasets import MNIST
from torchviz import make_dot


# (neptune) define model with logging (self.log)
class LitModel(pl.LightningModule):
    def __init__(self, linear_1, linear_2, learning_rate, decay_factor):
        super().__init__()
        self.linear = linear_1
        self.linear = linear_2
        self.learning_rate = learning_rate
        self.decay_factor = decay_factor
        self.layer_1 = torch.nn.Linear(28 * 28, linear_1)
        self.layer_2 = torch.nn.Linear(linear_1, linear_2)
        self.layer_3 = torch.nn.Linear(linear_2, 10)

    def forward(self, x):
        x = x.view(x.size(0), -1)
        x = self.layer_1(x)
        x = F.relu(x)
        x = self.layer_2(x)
        x = F.relu(x)
        x = self.layer_3(x)
        return x

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        scheduler = LambdaLR(optimizer, lambda epoch: self.decay_factor ** epoch)
        return [optimizer], [scheduler]

    def training_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)
        loss = F.cross_entropy(y_hat, y)
        self.log("train/batch/loss", loss, prog_bar=False)

        y_true = y.cpu().detach().numpy()
        y_pred = y_hat.argmax(axis=1).cpu().detach().numpy()
        acc = accuracy_score(y_true, y_pred)
        self.log("train/batch/acc", acc)

        return {"loss": loss,
                "y_true": y_true,
                "y_pred": y_pred}

    def training_epoch_end(self, outputs):
        loss = np.array([])
        y_true = np.array([])
        y_pred = np.array([])
        for results_dict in outputs:
            loss = np.append(loss, results_dict["loss"])
            y_true = np.append(y_true, results_dict["y_true"])
            y_pred = np.append(y_pred, results_dict["y_pred"])
        acc = accuracy_score(y_true, y_pred)
        self.log("train/epoch/loss", loss.mean())
        self.log("train/epoch/acc", acc)

    def validation_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)
        loss = F.cross_entropy(y_hat, y)

        y_true = y.cpu().detach().numpy()
        y_pred = y_hat.argmax(axis=1).cpu().detach().numpy()

        # example prediction
        img = np.squeeze(x[0].detach().cpu())
        img = img.mul_(0.3081).add_(0.1307).numpy()

        output = y_hat[0].detach().cpu()
        name = "pred: {}".format(y_pred[0])
        desc_target = "target: {}".format(y_true[0])
        desc_classes = "\n".join(["class {}: {}".format(j, pred)
                                 for j, pred in enumerate(F.softmax(output, dim=0))])
        description = "{} \n{}".format(desc_target, desc_classes)

        return {"loss": loss,
                "y_true": y_true,
                "y_pred": y_pred,
                "predictions": {"img": img,
                                "name": name,
                                "description": description}
                }

    def validation_epoch_end(self, outputs):
        loss = np.array([])
        y_true = np.array([])
        y_pred = np.array([])
        image_preds = np.array([])

        for results_dict in outputs:
            loss = np.append(loss, results_dict["loss"])
            y_true = np.append(y_true, results_dict["y_true"])
            y_pred = np.append(y_pred, results_dict["y_pred"])
            image_preds = np.append(image_preds, results_dict["predictions"])

        acc = accuracy_score(y_true, y_pred)
        self.log("val/loss", loss.mean())
        self.log("val/acc", acc)

        if self.current_epoch % 5 == 0:
            for data in image_preds:
                neptune_logger.experiment[f"val/preds/epoch_{self.current_epoch}"].log(
                    value=neptune.types.File.as_image(data["img"]),
                    name=data["name"],
                    description=data["description"],
                )

    def test_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)
        loss = F.cross_entropy(y_hat, y)

        y_true = y.cpu().detach().numpy()
        y_pred = y_hat.argmax(axis=1).cpu().detach().numpy()

        for j in np.where(np.not_equal(y_true, y_pred))[0]:
            img = np.squeeze(x[j].cpu().detach().numpy())
            img[img < 0] = 0
            img = img / np.amax(img)
            neptune_logger.experiment["test/misclassified_images"].log(
                neptune.types.File.as_image(img),
                description="y_pred={}, y_true={}".format(y_pred[j], y_true[j]),
            )

        return {"loss": loss,
                "y_true": y_true,
                "y_pred": y_pred}

    def test_epoch_end(self, outputs):
        loss = np.array([])
        y_true = np.array([])
        y_pred = np.array([])
        for results_dict in outputs:
            loss = np.append(loss, results_dict["loss"])
            y_true = np.append(y_true, results_dict["y_true"])
            y_pred = np.append(y_pred, results_dict["y_pred"])
        acc = accuracy_score(y_true, y_pred)
        self.log("test/loss", loss.mean())
        self.log("test/acc", acc)


# define DataModule
class MNISTDataModule(pl.LightningDataModule):
    def __init__(self, batch_size, normalization_vector):
        super().__init__()
        self.batch_size = batch_size
        self.normalization_vector = normalization_vector
        self.mnist_train = None
        self.mnist_val = None
        self.mnist_test = None

    def prepare_data(self):
        MNIST(os.getcwd(), train=True, download=True)
        MNIST(os.getcwd(), train=False, download=True)

    def setup(self, stage):
        # transforms
        transform = transforms.Compose([transforms.ToTensor(),
                                        transforms.Normalize(self.normalization_vector[0],
                                                             self.normalization_vector[1])])
        if stage == "fit":
            mnist_train = MNIST(os.getcwd(), train=True, transform=transform)
            self.mnist_train, self.mnist_val = random_split(mnist_train, [55000, 5000])
        if stage == "test":
            self.mnist_test = MNIST(os.getcwd(), train=False, transform=transform)

    def train_dataloader(self):
        mnist_train = DataLoader(self.mnist_train, batch_size=self.batch_size, num_workers=4)
        return mnist_train

    def val_dataloader(self):
        mnist_val = DataLoader(self.mnist_val, batch_size=self.batch_size, num_workers=4)
        return mnist_val

    def test_dataloader(self):
        mnist_test = DataLoader(self.mnist_test, batch_size=self.batch_size, num_workers=1)
        return mnist_test


# (neptune) log confusion matrix for classification
def log_confusion_matrix(lit_model, data_module):
    lit_model.freeze()
    test_data = data_module.test_dataloader()
    y_true = np.array([])
    y_pred = np.array([])
    for i, (x, y) in enumerate(test_data):
        y = y.cpu().detach().numpy()
        y_hat = lit_model.forward(x).argmax(axis=1).cpu().detach().numpy()
        y_true = np.append(y_true, y)
        y_pred = np.append(y_pred, y_hat)

    fig, ax = plt.subplots(figsize=(16, 12))
    plot_confusion_matrix(y_true, y_pred, ax=ax)
    neptune_logger.experiment["confusion_matrix"].upload(neptune.types.File.as_image(fig))


# (neptune) log model visualization
def log_model_visualization(lit_model, data_module):
    lit_model.freeze()
    td = data_module.train_dataloader()
    data = iter(td).next()
    y = lit_model(data[0])
    model_vis = make_dot(y.mean(), params=dict(lit_model.named_parameters()))
    model_vis.format = "png"
    model_vis.render("model_vis")
    neptune_logger.experiment["model/visualization"] = neptune.types.File("model_vis.png")


# load hyper-parameters
with open("parameters.yml", "r") as stream:
    parameters = yaml.safe_load(stream)

# create learning rate logger
lr_logger = LearningRateMonitor(logging_interval="epoch")

# create model checkpointing object
model_checkpoint = ModelCheckpoint(
    dirpath="model/checkpoints/",
    filename="{epoch:02d}",
    save_weights_only=True,
    save_top_k=3,
    save_last=True,
    monitor="val/loss",
    every_n_epochs=1,
)

# (neptune) create NeptuneLogger
neptune_logger = NeptuneLogger(
    project="common/webinar-pytorch-lightning",
    tags=["training", "mnist"],
)

# (neptune) initialize a trainer and pass neptune_logger
trainer = pl.Trainer(
    logger=neptune_logger,
    callbacks=[lr_logger, model_checkpoint],
    log_every_n_steps=50,
    max_epochs=parameters["max_epochs"],
    track_grad_norm=2,
)

# init model
model = LitModel(
    linear_1=parameters["linear_1"],
    linear_2=parameters["linear_2"],
    learning_rate=parameters["lr"],
    decay_factor=parameters["decay_factor"],
)

# init datamodule
dm = MNISTDataModule(
    normalization_vector=((0.1307,), (0.3081,)),
    batch_size=parameters["batch_size"],
)

# (neptune) log model summary
neptune_logger.log_model_summary(model=model, max_depth=-1)

# (neptune) log hyper-parameters
neptune_logger.log_hyperparams(params=parameters)

# train and test the model, log metadata to the Neptune run
trainer.fit(model, datamodule=dm)
trainer.test(model, datamodule=dm)

# (neptune) log confusion matrix
log_confusion_matrix(model, dm)

# (neptune) log model visualization
log_model_visualization(model, dm)
