import torch
from pytorch_lightning import LightningModule
from objdetecteval.metrics.coco_metrics import get_coco_stats
from transforms import get_train_transforms, get_valid_transforms
from create_model import create_model
from fastcore.dispatch import typedispatch
from typing import List
import numpy as np
from ensemble_boxes import ensemble_boxes_wbf

def run_wbf(predictions, image_size=512, iou_thr=0.44, skip_box_thr=0.43, weights=None):
    bboxes = []
    confidences = []
    class_labels = []

    for prediction in predictions:
        boxes = [(prediction["boxes"] / image_size).tolist()]
        scores = [prediction["scores"].tolist()]
        labels = [prediction["classes"].tolist()]

        boxes, scores, labels = ensemble_boxes_wbf.weighted_boxes_fusion(
            boxes,
            scores,
            labels,
            weights=weights,
            iou_thr=iou_thr,
            skip_box_thr=skip_box_thr,
        )
        boxes = boxes * (image_size - 1)
        bboxes.append(boxes.tolist())
        confidences.append(scores.tolist())
        class_labels.append(labels.tolist())

    return bboxes, confidences, class_labels

class EfficientDetModel(LightningModule):
    def __init__(
        self,
        num_classes=1,
        img_size=512,
        prediction_confidence_threshold=0.2,
        learning_rate=0.001,
        gamma=0.8,
        wbf_iou_threshold=0.44,
        inference_transforms=get_valid_transforms(target_img_size=512),
        model_architecture='tf_efficientnetv2_l',
    ):
        super().__init__()
        self.img_size = img_size
        self.model = create_model(
            num_classes, img_size, architecture=model_architecture
        )
        self.prediction_confidence_threshold = prediction_confidence_threshold
        self.lr = learning_rate
        self.wbf_iou_threshold = wbf_iou_threshold
        self.inference_tfms = inference_transforms
        self.validation_step_outputs = []
        self.validation_step_preds = []

    # @auto_move_data
    def forward(self, images, targets):
        return self.model(images, targets)

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.model.parameters(), lr=self.lr)
        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer=optimizer, gamma=0.8)
        
        return [optimizer], [scheduler]


    def training_step(self, batch, batch_idx):
        images, annotations, _, image_ids = batch

        losses = self.model(images, annotations)

        logging_losses = {
            "class_loss": losses["class_loss"].detach(),
            "box_loss": losses["box_loss"].detach(),
        }

        self.log_dict(
            {
                "train_loss": losses["loss"],
                "step": self.current_epoch
            },
            prog_bar=True,
            on_epoch=True,
            on_step=False,
            logger=True
        )

        self.log_dict(
            {
                "train_box_loss": losses["box_loss"],
                "step": self.current_epoch
            },
            prog_bar=True,
            on_epoch=True,
            on_step=False,
            logger=True
        )

        self.log_dict(
            {
                "train_class_loss": losses["class_loss"],
                "step": self.current_epoch
            },
            prog_bar=True,
            on_epoch=True,
            on_step=False,
            logger=True
        )



        # self.log("train_loss", losses["loss"], on_step=False, on_epoch=True, prog_bar=True,
        #          logger=True)
        # self.log("train_loss_step", losses["loss"], on_step=True, on_epoch=False, prog_bar=True,
        #          logger=True,  )
        
        # self.log(
        #     "train_class_loss", losses["class_loss"], on_step=False, on_epoch=True, prog_bar=True,
        #     logger=True
        # )
        # self.log("train_box_loss", losses["box_loss"], on_step=False, on_epoch=True, prog_bar=True,
        #          logger=True)

        return losses['loss']


    @torch.no_grad()
    def validation_step(self, batch, batch_idx):
        images, annotations, targets, image_ids = batch
        outputs = self.model(images, annotations)

        detections = outputs["detections"]

        batch_predictions = {
            "predictions": detections,
            "targets": targets,
            "image_ids": image_ids,
        }

        logging_losses = {
            "class_loss": outputs["class_loss"].detach(),
            "box_loss": outputs["box_loss"].detach(),
        }

        self.log_dict(
            {
                "val_loss": outputs["loss"],
                "step": self.current_epoch
            },
            prog_bar=True,
            on_epoch=True,
            on_step=False,
            logger=True
        )

        self.log_dict(
            {
                "val_box_loss": outputs["box_loss"],
                "step": self.current_epoch
            },
            prog_bar=True,
            on_epoch=True,
            on_step=False,
            logger=True
        )

        self.log_dict(
            {
                "val_class_loss": outputs["class_loss"],
                "step": self.current_epoch
            },
            prog_bar=True,
            on_epoch=True,
            on_step=False,
            logger=True
        )

        # self.log("val_loss", outputs["loss"], on_step=False, on_epoch=True, prog_bar=True,
        #          logger=True, sync_dist=True)
        
        # self.log("val_loss_step", outputs["loss"], on_step=True, on_epoch=False, prog_bar=True,
        #          logger=True, sync_dist=True)
        # self.log(
        #     "val_class_loss", logging_losses["class_loss"], on_step=False, on_epoch=True,
        #     prog_bar=True, logger=True, sync_dist=True
        # )
        # self.log("val_box_loss", logging_losses["box_loss"], on_step=False, on_epoch=True,
        #          prog_bar=True, logger=True, sync_dist=True)
        
        self.validation_step_outputs.append(outputs['loss'])
        self.validation_step_preds.append({'loss': outputs["loss"], 'batch_predictions': batch_predictions})

        return {'loss': outputs["loss"], 'batch_predictions': batch_predictions}
    
    @typedispatch
    def predict(self, images: List):
        """
        For making predictions from images
        Args:
            images: a list of PIL images

        Returns: a tuple of lists containing bboxes, predicted_class_labels, predicted_class_confidences

        """
        image_sizes = [(image.size[1], image.size[0]) for image in images]
        images_tensor = torch.stack(
            [
                self.inference_tfms(
                    image=np.array(image, dtype=np.float32),
                    labels=np.ones(1),
                    bboxes=np.array([[0, 0, 1, 1]]),
                )["image"]
                for image in images
            ]
        )

        return self._run_inference(images_tensor, image_sizes)

    @typedispatch
    def predict(self, images_tensor: torch.Tensor):
        """
        For making predictions from tensors returned from the model's dataloader
        Args:
            images_tensor: the images tensor returned from the dataloader

        Returns: a tuple of lists containing bboxes, predicted_class_labels, predicted_class_confidences

        """
        if images_tensor.ndim == 3:
            images_tensor = images_tensor.unsqueeze(0)
        if (
            images_tensor.shape[-1] != self.img_size
            or images_tensor.shape[-2] != self.img_size
        ):
            raise ValueError(
                f"Input tensors must be of shape (N, 3, {self.img_size}, {self.img_size})"
            )

        num_images = images_tensor.shape[0]
        image_sizes = [(self.img_size, self.img_size)] * num_images

        return self._run_inference(images_tensor, image_sizes)

    def _run_inference(self, images_tensor, image_sizes):
        dummy_targets = self._create_dummy_inference_targets(
            num_images=images_tensor.shape[0]
        )

        detections = self.model(images_tensor.to(self.device), dummy_targets)[
            "detections"
        ]
        (
            predicted_bboxes,
            predicted_class_confidences,
            predicted_class_labels,
        ) = self.post_process_detections(detections)

        scaled_bboxes = self.__rescale_bboxes(
            predicted_bboxes=predicted_bboxes, image_sizes=image_sizes
        )

        return scaled_bboxes, predicted_class_labels, predicted_class_confidences
    
    def _create_dummy_inference_targets(self, num_images):
        dummy_targets = {
            "bbox": [
                torch.tensor([[0.0, 0.0, 0.0, 0.0]], device=self.device)
                for i in range(num_images)
            ],
            "cls": [torch.tensor([1.0], device=self.device) for i in range(num_images)],
            "img_size": torch.tensor(
                [(self.img_size, self.img_size)] * num_images, device=self.device
            ).float(),
            "img_scale": torch.ones(num_images, device=self.device).float(),
        }

        return dummy_targets
    
    def post_process_detections(self, detections):
        predictions = []
        for i in range(detections.shape[0]):
            predictions.append(
                self._postprocess_single_prediction_detections(detections[i])
            )

        predicted_bboxes, predicted_class_confidences, predicted_class_labels = run_wbf(
            predictions, image_size=self.img_size, iou_thr=self.wbf_iou_threshold
        )

        return predicted_bboxes, predicted_class_confidences, predicted_class_labels

    def _postprocess_single_prediction_detections(self, detections):
        boxes = detections.detach().cpu().numpy()[:, :4]
        scores = detections.detach().cpu().numpy()[:, 4]
        classes = detections.detach().cpu().numpy()[:, 5]
        indexes = np.where(scores > self.prediction_confidence_threshold)[0]
        boxes = boxes[indexes]

        return {"boxes": boxes, "scores": scores[indexes], "classes": classes[indexes]}

    def __rescale_bboxes(self, predicted_bboxes, image_sizes):
        scaled_bboxes = []
        for bboxes, img_dims in zip(predicted_bboxes, image_sizes):
            im_h, im_w = img_dims

            if len(bboxes) > 0:
                scaled_bboxes.append(
                    (
                        np.array(bboxes)
                        * [
                            im_w / self.img_size,
                            im_h / self.img_size,
                            im_w / self.img_size,
                            im_h / self.img_size,
                        ]
                    ).tolist()
                )
            else:
                scaled_bboxes.append(bboxes)

        return scaled_bboxes
    
    def aggregate_prediction_outputs(self, outputs):

        # for output in outputs:
        #     print(output)
            
        detections = torch.cat(
            [output["batch_predictions"]["predictions"] for output in outputs]
        )

        image_ids = []
        targets = []
        for output in outputs:
            batch_predictions = output["batch_predictions"]
            image_ids.extend(batch_predictions["image_ids"])
            targets.extend(batch_predictions["targets"])

        (
            predicted_bboxes,
            predicted_class_confidences,
            predicted_class_labels,
        ) = self.post_process_detections(detections)

        return (
            predicted_class_labels,
            image_ids,
            predicted_bboxes,
            predicted_class_confidences,
            targets,
        )
    
    def on_validation_epoch_end(self):
        """
        Compute and log training loss and accuracy at the epoch level.
        """

        # validation_loss_mean = torch.stack(
        #     [output["loss"] for output in outputs]
        # ).mean()

        validation_loss_mean = torch.stack(self.validation_step_outputs).mean()
        self.log("validation_epoch_average", validation_loss_mean)
        

        (
            predicted_class_labels,
            image_ids,
            predicted_bboxes,
            predicted_class_confidences,
            targets,
        ) = self.aggregate_prediction_outputs(self.validation_step_preds)

        truth_image_ids = [target["image_id"].detach().item() for target in targets]
        truth_boxes = [
            target["bboxes"].detach()[:, [1, 0, 3, 2]].tolist() for target in targets
        ] # convert to xyxy for evaluation
        truth_labels = [target["labels"].detach().tolist() for target in targets]

        stats = get_coco_stats(
            prediction_image_ids=image_ids,
            predicted_class_confidences=predicted_class_confidences,
            predicted_bboxes=predicted_bboxes,
            predicted_class_labels=predicted_class_labels,
            target_image_ids=truth_image_ids,
            target_bboxes=truth_boxes,
            target_class_labels=truth_labels,
        )['All']

        self.validation_step_outputs.clear()
        self.validation_step_preds.clear()

        return {"val_loss": validation_loss_mean, "metrics": stats}