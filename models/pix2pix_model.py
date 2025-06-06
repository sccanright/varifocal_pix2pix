import torch
from .base_model import BaseModel
from . import networks


def laplace_nll(y_true, y_pred):
    C = torch.log(torch.tensor(2.0))
    n = y_true.shape[1]
    if y_pred.shape[1] != 2 * n:
        raise ValueError(f"Expected y_pred to have {2 * n} channels, but got {y_pred.shape[1]} channels.")
        
    mu = y_pred[:, :n, :, :]
    sigma = y_pred[:, n:, :, :]

    # Ensure sigma is positive and above a minimum threshold
    sigma = torch.clamp(sigma, min=1e-3)
    
    # Compute the negative log-likelihood
    nll = torch.abs((mu - y_true) / sigma) + 0.158 + C
    nll_mean = torch.mean(nll)
    
    return nll_mean

def laplace_pdf (y_true, y_pred):
    n = y_true.shape[1]
    if y_pred.shape[1] != 2 * n:
        raise ValueError(f"Expected y_pred to have {2 * n} channels, but got {y_pred.shape[1]} channels.")
    
    mu = y_pred[:, :n, :, :]
    sigma = y_pred[:, n:, :, :]
    
    # Ensure sigma is positive and above a minimum threshold
    sigma = torch.clamp(sigma, min=1e-3)
    
    # Compute the probability density function
    pdf = 1 / (2 * sigma) * torch.exp(-torch.abs(y_true - mu) / sigma)
    
    pdf_mean = torch.mean(pdf)

    pdf_loss = torch.abs(torch.abs(pdf_mean) - 0.6931471805599453) # subtract natural log of 2 which is 25 and 75th percentile of laplace distribution

    #print(f"pdf_loss: {pdf_loss}")

    L1 = torch.mean(torch.abs(y_true - mu))

    return L1 + pdf_loss/100

class Pix2PixModel(BaseModel):
    """ This class implements the pix2pix model, for learning a mapping from input images to output images given paired data.

    The model training requires '--dataset_mode aligned' dataset.
    By default, it uses a '--netG unet256' U-Net generator,
    a '--netD basic' discriminator (PatchGAN),
    and a '--gan_mode' vanilla GAN loss (the cross-entropy objective used in the orignal GAN paper).

    pix2pix paper: https://arxiv.org/pdf/1611.07004.pdf
    """
    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        """Add new dataset-specific options, and rewrite default values for existing options.

        Parameters:
            parser          -- original option parser
            is_train (bool) -- whether training phase or test phase. You can use this flag to add training-specific or test-specific options.

        Returns:
            the modified parser.

        For pix2pix, we do not use image buffer
        The training objective is: GAN Loss + lambda_L1 * ||G(A)-B||_1
        By default, we use vanilla GAN loss, UNet with batchnorm, and aligned datasets.
        """
        # changing the default values to match the pix2pix paper (https://phillipi.github.io/pix2pix/)
        parser.set_defaults(norm='batch', netG='unet_256', dataset_mode='aligned')
        if is_train:
            parser.set_defaults(pool_size=0, gan_mode='vanilla')
            parser.add_argument('--lambda_L1', type=float, default=100.0, help='weight for L1 loss')

        return parser

    def __init__(self, opt):
        """Initialize the pix2pix class.

        Parameters:
            opt (Option class)-- stores all the experiment flags; needs to be a subclass of BaseOptions
        """
        BaseModel.__init__(self, opt)
        # specify the training losses you want to print out. The training/test scripts will call <BaseModel.get_current_losses>
        # add G_NLL
        self.loss_names = ['G_GAN', 'G_L1', 'D_real', 'D_fake']
        # specify the images you want to save/display. The training/test scripts will call <BaseModel.get_current_visuals>
        self.visual_names = ['real_A', 'fake_B', 'real_B']
        # specify the models you want to save to the disk. The training/test scripts will call <BaseModel.save_networks> and <BaseModel.load_networks>
        if self.isTrain:
            self.model_names = ['G', 'D']
        else:  # during test time, only load G
            self.model_names = ['G']
        # define networks (both generator and discriminator)
        self.netG = networks.define_G(opt.input_nc, opt.output_nc, opt.ngf, opt.netG, opt.norm,
                                      not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)
        # change for mean and scale outputs
        #self.netG = networks.define_G(opt.input_nc, opt.output_nc * 2, opt.ngf, opt.netG, opt.norm,
        #                               not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)


        if self.isTrain:  
            # define a discriminator; conditional GANs need to take both input and output images; Therefore, #channels for D is input_nc + output_nc
            self.netD = networks.define_D(opt.input_nc + opt.output_nc, opt.ndf, opt.netD,
                                          opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, self.gpu_ids)

        if self.isTrain:
            # define loss functions
            self.criterionGAN = networks.GANLoss(opt.gan_mode).to(self.device)
            self.criterionL1 = torch.nn.L1Loss()

            # add NLL
            #self.criterionNLL = laplace_nll


            # add PDF
            # self.criterionNLL = laplace_pdf 

            # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
            self.optimizer_G = torch.optim.Adam(self.netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(self.netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)

    def set_input(self, input):
        """Unpack input data from the dataloader and perform necessary pre-processing steps.

        Parameters:
            input (dict): include the data itself and its metadata information.

        The option 'direction' can be used to swap images in domain A and domain B.
        """
        
        AtoB = self.opt.direction == 'AtoB'
        self.real_A = input['A' if AtoB else 'B'].to(self.device)
        self.real_B = input['B' if AtoB else 'A'].to(self.device)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']
        self.real_A = self.real_A.squeeze(0)        ## Otherwise real_A was one dimension too big :O
        #####print(F"Max p2p: {torch.max(self.real_B)}")
        ####print(F"Min p2p: {torch.min(self.real_B)}")
        ###print(f"Real A: {self.real_A.shape}")
        ###print(f"Real B: {self.real_B.shape}")

    #def forward(self):
    #    """Run forward pass; called by both functions <optimize_parameters> and <test>."""
    #    self.fake_B = self.netG(self.real_A)  # G(A)

    #def forward(self):
    #    output = self.netG(self.real_A)  # G(real)
    #    self.fake_mean = output[:, :self.real_A.shape[1], :, :]  # Mean
    #    self.fake_scale = output[:, self.real_A.shape[1]:, :, :]  # Scale

    def forward(self):
        """Run forward pass."""
        #output = self.netG(self.real_A)  # G(real)
        
        # Ensure the output has the correct shape
        #assert output.shape[1] == self.real_A.shape[1] * 2, \
        #    f"Output channels {output.shape[1]} do not match expected channels {self.real_A.shape[1] * 2}"
        
        #self.fake_mean = output[:, :self.real_A.shape[1], :, :]  # Mean
        #self.fake_scale = output[:, self.real_A.shape[1]:, :, :]  # Scale
        self.fake_B = self.netG(self.real_A)  # G(A)

        # Debugging statements
        #print(f"output shape: {output.shape}")
        #print(f"fake_mean shape: {self.fake_mean.shape}")
        #print(f"fake_scale shape: {self.fake_scale.shape}")

    def backward_D(self):
        """Calculate GAN loss for the discriminator"""
        # Fake; stop backprop to the generator by detaching fake_B
        #fake_AB = torch.cat((self.real_A, self.fake_B), 1)  # we use conditional GANs; we need to feed both input and output to the discriminator
        # discriminator looks at fake_mean which is only one channel
        #fake_AB = torch.cat((self.real_A, self.fake_B), 1)  # Use only mean for D
        
        #pred_fake = self.netD(fake_AB.detach())
        #self.loss_D_fake = self.criterionGAN(pred_fake, False)
        self.loss_D_fake = 0
        # Real
        #real_AB = torch.cat((self.real_A, self.real_B), 1)
        #pred_real = self.netD(real_AB)
        #self.loss_D_real = self.criterionGAN(pred_real, True)
        self.loss_D_real = 0
        # combine loss and calculate gradients
        self.loss_D = (self.loss_D_fake + self.loss_D_real) * 0.5

        #self.loss_D.backward()

    def backward_G(self):
        """Calculate GAN and L1 loss for the generator"""
        # First, G(A) should fake the discriminator
        #fake_AB = torch.cat((self.real_A, self.fake_B), 1)
        # Calculate and combine GAN loss, L1 loss, and NLL loss using only the mean channel.
        #fake_AB = torch.cat((self.real_A, self.fake_B), 1)

        #pred_fake = self.netD(fake_AB)
        #self.loss_G_GAN = self.criterionGAN(pred_fake, True)
        self.loss_G_GAN = 0
        # Second, G(A) = B
        #self.loss_G_L1 = self.criterionL1(self.fake_B, self.real_B) * self.opt.lambda_L1
        # use only the mean part of real_B of mean and scale
        #print(f'fake mean shape{self.fake_mean.shape}')
        #print(f'real mean shape{self.real_B.shape}')

        self.loss_G_L1 = self.criterionL1(self.fake_B, self.real_B) * self.opt.lambda_L1


        # Add NLL loss
        #self.loss_G_NLL = self.criterionNLL(self.real_B, self.fake_mean)
        #self.loss_G_NLL = self.criterionNLL(self.real_B, self.fake_B)
        #self.loss_G_PDF = self.criterionPDF(self.real_B, self.fake_B)   
        # combine loss and calculate gradients new with NLL
        #self.loss_G = self.loss_G_GAN + self.loss_G_L1 + self.loss_G_NLL  # Combine all losses
#        self.loss_G = torch.log(self.loss_G_GAN / 10) + self.loss_G_NLL  # Combine all losses

        #self.loss_G = self.loss_G_GAN/100 + self.loss_G_NLL  # Combine all losses
        #self.loss_G = self.loss_G_NLL  # Combine all losses
        #self.loss_G = self.loss_G_NLL  # Al PDF loss   
        # combine loss and calculate gradients old
        self.loss_G = self.loss_G_L1
        self.loss_G.backward()

    

    def optimize_parameters(self):
        self.forward()                   # compute fake images: G(A)
        # update D
        self.set_requires_grad(self.netD, True)  # enable backprop for D
        self.optimizer_D.zero_grad()     # set D's gradients to zero
        self.backward_D()                # calculate gradients for D
        self.optimizer_D.step()          # update D's weights
        # update G
        self.set_requires_grad(self.netD, False)  # D requires no gradients when optimizing G
        self.optimizer_G.zero_grad()        # set G's gradients to zero
        self.backward_G()                   # calculate graidents for G
        self.optimizer_G.step()             # update G's weights
