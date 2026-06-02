


from bioio import BioImage
import  bioio_czi
import numpy as np
from cellpose import models, plot

import matplotlib.pyplot as plt


path = "C:/Users/zeiss/Zeiss_OAD/OAD/ZEN-API/python_examples/data/Snap-357.czi"


img = BioImage(path)

data = img.data
print(data.shape)
print(img.dims)


cp_img = img.get_image_data("YX", T=0, C=0, Z=0).astype(np.float32)  # 2D YX slice


#model = models.CellposeModel(gpu=True, pretrained_nuclei=True)  # or "nuclei", etc.
#model = models.CellposeModel(gpu=True, pretrained_model="nuclei")
model = models.CellposeModel(gpu=True)


print("DONE")


masks, flows, styles = model.eval(
    cp_img,
    #diameter=20,          # let Cellpose estimate, or set pixel size
    #channels=[0, 0],        # grayscale image
    do_3D=False,
)

print(f"Found {len(np.unique(masks))-1} nuclei")

fig = plt.figure(figsize=(12,5))
plot.show_segmentation(fig, cp_img, masks, flows[0])
plt.tight_layout()
plt.show()

