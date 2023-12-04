import torch
import torch.nn as nn
from typing import Tuple
from torch.nn import init

class BockNet(nn.Module):
    def __init__(self, blindspot=7, nch_in=189, nch_out=189, nch_ker=64):
        super(BockNet, self).__init__()
        in_channels = nch_in
        out_channels = nch_out
        dim = nch_ker
        self.blindspot = blindspot
        self.Conv2d = ShiftConv2d if self.blindspot > 0 else nn.Conv2d
        
        # Encode Blocks

        # Layers: enc_conv0, enc_conv1, pool1
        self.encode_block_1 = nn.Sequential(
            self.Conv2d(in_channels, dim, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(dim, dim, 3, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
        )
        
        # Layers: downsample2
        if self.blindspot > 0:
            self.encode_block_2 = nn.Sequential(
                Shift2d((1, 0)),
                nn.MaxPool2d(2),
            )
        else:
            self.encode_block_2 = nn.MaxPool2d(2)
            
        # Layers: enc_conv(i); i=3,4
        def _encode_block_3_4() -> nn.Module:
            return nn.Sequential(
                self.Conv2d(dim, dim, 3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.1, inplace=True),
            )

        # Same encode module
        self.encode_block_3 = _encode_block_3_4()
        self.encode_block_4 = _encode_block_3_4()

        # Layers: enc_conv5
        self.encode_block_5 = nn.Sequential(
            self.Conv2d(dim, dim//2, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
        )
        
        # Decode Blocks
        
        # Layers: dec_conv5
        self.decode_block_5 = nn.Sequential(
            self.Conv2d(dim//2, dim, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
        )

        # Layers: dec_deconv(i)a, dec_deconv(i)b; i=4,3
        def _decode_block_4_3() -> nn.Module:
            return nn.Sequential(
                self.Conv2d(2 * dim, dim, 3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.1, inplace=True),
                self.Conv2d(dim, dim, 3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.1, inplace=True),
            )

        # Same decode module
        self.decode_block_4 = _decode_block_4_3()
        self.decode_block_3 = _decode_block_4_3()
        
        # Layers: upsample2
        self.decode_block_2 = nn.Sequential(nn.Upsample(scale_factor=2, mode="nearest"))
        
        # Layers: dec_conv1a, dec_conv1b
        self.decode_block_1 = nn.Sequential(
            self.Conv2d(2 * dim, dim, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(dim, dim, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
        )
        
        # Output Block

        if self.blindspot > 0:
            # Shift blindspot pixel
            self.shift = Shift2d(((self.blindspot + 1) // 2, 0))
            # 4 x Channels due to batch cat
            dim_all = 4 * dim
        else:
            dim_all = dim
            
        self.output_conv = self.Conv2d(dim_all // 2, out_channels, 1)
        self.output_block = nn.Sequential(
            self.Conv2d(dim_all, dim_all, 1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(dim_all, dim_all // 2, 1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.output_conv,
        )

        # Initialize weights
        self.init_weights()

    def init_weights(self):
        """Initializes weights using Kaiming He et al. (2015).

        Only convolution layers have learnable weights. All convolutions use a leaky
        relu activation function (negative_slope = 0.1) except the last which is just
        a linear output.
        """
        with torch.no_grad():
            self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight.data, a=0.1)
                m.bias.data.zero_()
        # Initialise last output layer
        nn.init.kaiming_normal_(self.output_conv.weight.data, nonlinearity="linear")

    def forward(self, x):
        if self.blindspot > 0:
            rotated = [rotate(x, rot) for rot in (0, 90, 180, 270)]
            x = torch.cat((rotated), dim=0)

        # Encoder
        pool1 = self.encode_block_1(x)
        
        pool2 = self.encode_block_2(pool1)
        
        pool3 = self.encode_block_3(pool2)
        pool4 = self.encode_block_4(pool3)
        encoded = self.encode_block_5(pool4)

        # Decoder
        upsample5 = self.decode_block_5(encoded)
        concat5 = torch.cat((upsample5, pool4), dim=1)
        upsample4 = self.decode_block_4(concat5)
        concat4 = torch.cat((upsample4, pool3), dim=1)
        upsample3 = self.decode_block_3(concat4)
        
        upsample2 = self.decode_block_2(upsample3)
        
        concat2 = torch.cat((upsample2, pool1), dim=1)
        upsample1 = self.decode_block_1(concat2)
        
        # Output
        if self.blindspot > 0:  
            # Apply shift
            shifted = self.shift(upsample1)
            # Unstack, rotate and cat
            rotated_batch = torch.chunk(shifted, 4, dim=0)
            aligned = [
                rotate(rotated, rot)
                for rotated, rot in zip(rotated_batch, (0, 270, 180, 90))
            ]
            x_r = torch.cat(aligned, dim=1)
        else:
            x_r = upsample1
        
        x_r = self.output_block(x_r)

        return x_r
    
    
def rotate(x, angle):
    """Rotate images by 90 degrees clockwise. Can handle any 2D data format.
    Args:
        x (Tensor): Image or batch of images.
        angle (int): Clockwise rotation angle in multiples of 90.
        data_format (str, optional): Format of input image data, e.g. BCHW,
            HWC. Defaults to BCHW.
    Returns:
        Tensor: Copy of tensor with rotation applied.
    """
    h_dim, w_dim = 2, 3

    if angle == 0:
        return x
    elif angle == 90:
        return x.flip(w_dim).transpose(h_dim, w_dim)
    elif angle == 180:
        return x.flip(w_dim).flip(h_dim)
    elif angle == 270:
        return x.flip(h_dim).transpose(h_dim, w_dim)
    else:
        raise NotImplementedError("Must be rotation divisible by 90 degrees")

class Crop2d(nn.Module):
    """Crop input using slicing. Assumes BCHW data.

    Args:
        crop (Tuple[int, int, int, int]): Amounts to crop from each side of the image.
            Tuple is treated as [left, right, top, bottom]/
    """

    def __init__(self, crop: Tuple[int, int, int, int]):
        super().__init__()
        self.crop = crop
        assert len(crop) == 4

    def forward(self, x):
        (left, right, top, bottom) = self.crop
        x0, x1 = left, x.shape[-1] - right
        y0, y1 = top, x.shape[-2] - bottom
        return x[:, :, y0:y1, x0:x1]


class Shift2d(nn.Module):
    """Shift an image in either or both of the vertical and horizontal axis by first
    zero padding on the opposite side that the image is shifting towards before
    cropping the side being shifted towards.

    Args:
        shift (Tuple[int, int]): Tuple of vertical and horizontal shift. Positive values
            shift towards right and bottom, negative values shift towards left and top.
    """

    def __init__(self, shift: Tuple[int, int]):
        super().__init__()
        self.shift = shift
        vert, horz = self.shift
        y_a, y_b = abs(vert), 0
        x_a, x_b = abs(horz), 0
        if vert < 0:
            y_a, y_b = y_b, y_a
        if horz < 0:
            x_a, x_b = x_b, x_a
        # Order : Left, Right, Top Bottom
        self.pad = nn.ZeroPad2d((x_a, x_b, y_a, y_b))
        self.crop = Crop2d((x_b, x_a, y_b, y_a))
        self.shift_block = nn.Sequential(self.pad, self.crop)

    def forward(self, x):
        return self.shift_block(x)


class ShiftConv2d(nn.Conv2d):
    def __init__(self, *args, **kwargs):
        """Custom convolution layer as defined by Laine et al. for restricting the
        receptive field of a convolution layer to only be upwards. For a h × w kernel,
        a downwards offset of k = [h/2] pixels is used. This is applied as a k sized pad
        to the top of the input before applying the convolution. The bottom k rows are
        cropped out for output.
        """
        super().__init__(*args, **kwargs)
        self.shift_size = (self.kernel_size[0] // 2, 0)
        # Use individual layers of shift for wrapping conv with shift
        shift = Shift2d(self.shift_size)
        self.pad = shift.pad
        self.crop = shift.crop

    def forward(self, x):
        x = self.pad(x)
        x = super().forward(x)
        x = self.crop(x)
        return x
