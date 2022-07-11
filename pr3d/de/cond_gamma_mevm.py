import tensorflow as tf
import keras
import numpy as np
import numpy.typing as npt
from scipy import optimize
import tensorflow_probability as tfp
import tensorflow_addons as tfa
import h5py
from typing import Tuple
import numpy.typing as npt
import numpy as np

tfd = tfp.distributions

from pr3d.common.evm import *
from pr3d.common.core import ConditionalDensityEstimator

# in order to use tfd.Gamma.quantile
#tf.compat.v1.disable_eager_execution()

class ConditionalGammaMixtureEVM(ConditionalDensityEstimator):
    def __init__(
        self,
        centers : int = 3,
        x_dim : list = None,
        h5_addr : str = None,
        bayesian : bool = False,
        batch_size : int = None,
        dtype : str = 'float64',
        hidden_sizes = (16,16), 
        hidden_activation = 'tanh',
    ):

        super(ConditionalGammaMixtureEVM,self).__init__(
            x_dim = x_dim,
            h5_addr = h5_addr,
            bayesian = bayesian,
            batch_size = batch_size,
            dtype = dtype,
            hidden_sizes = hidden_sizes,
            hidden_activation = hidden_activation,
        )

        # figure out parameters
        if h5_addr is not None:
            # read side parameters
            with h5py.File(h5_addr, 'r') as hf:
                self._x_dim = [ encoded.decode("utf-8")  for encoded in list(hf.get('x_dim')[0])]
                self._centers = int(hf.get('centers')[0])
                self._bayesian = bool(hf.get('bayesian')[0])

                if 'batch_size' in hf.keys():
                    self._batch_size = int(hf.get('batch_size')[0])
                
                if 'hidden_sizes' in hf.keys():
                    self._hidden_sizes = tuple(hf.get('hidden_sizes')[0])

                if 'hidden_activation' in hf.keys():
                    self._hidden_activation = str(hf.get('hidden_activation')[0].decode("utf-8"))

        else:
            self._x_dim = x_dim
            self._centers = centers
            self._bayesian = bayesian
            self._batch_size = batch_size
            self._hidden_sizes = hidden_sizes
            self._hidden_activation = hidden_activation


        # create parameters dict
        self._params_config = {
            'mixture_gamma_weights': { 
                'slice_size' : self.centers,
                'slice_activation' : 'softmax',
            },
            'mixture_gamma_shapes': { 
                'slice_size' : self.centers,
                'slice_activation' : 'softplus',
            },
            'mixture_gamma_rates': { 
                'slice_size' : self.centers,
                'slice_activation' : 'softplus',
            },
            'tail_parameter' : {
                'slice_size' : 1,
                'slice_activation' : 'softplus',
            },
            'tail_threshold' : {
                'slice_size' : 1,
                'slice_activation' : 'softplus',
            },
            'tail_scale' : {
                'slice_size' : 1,
                'slice_activation' : 'softplus',
            },
        }

        # ask ConditionalDensityEstimator to form the MLP
        self.create_core(h5_addr = h5_addr)
        #self.core_model.model.summary()

        # create models for inference: 
        # self._prob_pred_model, self._sample_model, self._params_model, self._training_model
        self.create_models()

    def save(self, h5_addr : str) -> None:
        self.core_model.model.save(h5_addr)
        with h5py.File(h5_addr, 'a') as hf:
            hf.create_dataset('x_dim', shape=(1,len(self.x_dim)), data=self.x_dim)
            hf.create_dataset('centers', shape=(1,), data=int(self.centers))
            hf.create_dataset('bayesian', shape=(1,), data=int(self.bayesian))

            if self.batch_size is not None:
                hf.create_dataset('batch_size', shape=(1,), data=int(self.batch_size))

            if self.hidden_sizes is not None:
                hf.create_dataset('hidden_sizes', shape=(1,len(self.hidden_sizes)), data=list(self.hidden_sizes))

            if self.hidden_activation is not None:
                hf.create_dataset('hidden_activation', shape=(1,), data=str(self.hidden_activation))

    def create_models(self):

        # define X input
        self.x_input = list(self.core_model.input_slices.values())

        # put mixture components together
        self.mixture_gamma_weights = self.core_model.output_slices['mixture_gamma_weights']
        self.mixture_gamma_shapes = self.core_model.output_slices['mixture_gamma_shapes']
        self.mixture_gamma_rates = self.core_model.output_slices['mixture_gamma_rates']
        self.tail_param = self.core_model.output_slices['tail_parameter']
        self.tail_threshold = self.core_model.output_slices['tail_threshold']
        self.tail_scale = self.core_model.output_slices['tail_scale']

        # these models are used for printing paramters
        self._params_model = keras.Model(
            #inputs=list(self.x_input.values()),
            #inputs=self.x_input,
            inputs = {**self.core_model.input_slices},
            outputs=[
                self.mixture_gamma_weights,
                self.mixture_gamma_shapes,
                self.mixture_gamma_rates,
                self.tail_param,
                self.tail_threshold,
                self.tail_scale,
            ],
            name="params_model",
        )


        # create gamma mixture
        cat = tfd.Categorical(probs=self.mixture_gamma_weights,dtype=self.dtype)
        components = [tfd.Gamma(concentration=shape, rate=rate) for shape, rate
                        in zip(tf.unstack(self.mixture_gamma_shapes, axis=1), tf.unstack(self.mixture_gamma_rates, axis=1))]
        mixture = tfd.Mixture(cat=cat, components=components)

        # find the normalization factor (from X)
        # squeezing the tail_threshold was important
        self.norm_factor = tf.constant(1.00,dtype=self.dtype)-mixture.cdf(tf.squeeze(self.tail_threshold))

        # define Y input
        self.y_input = keras.Input(
            name = "y_input",
            shape=(1),
            batch_size = self.batch_size,
            dtype=self.dtype,
        )

        # create batch size tensor (from Y)
        self.y_batchsize = tf.cast(tf.size(self.y_input),dtype=self.dtype)

        # split the values into bulk and tail according to the tail_threshold (from X and Y)
        bool_split_tensor, tail_samples_count, bulk_samples_count = split_bulk_gpd(
            tail_threshold = self.tail_threshold,
            y_input = self.y_input,
            y_batch_size = self.y_batchsize,
            dtype = self.dtype,
        )

        # define bulk probabilities (from X and Y)
        bulk_prob_t = mixture.prob(tf.squeeze(self.y_input))
        bulk_cdf_t = mixture.cdf(tf.squeeze(self.y_input))
        bulk_tail_prob_t = tf.constant(1.00,dtype=self.dtype)-bulk_cdf_t


        # define GPD probabilities (from X and Y)
        gpd_prob_t = gpd_prob(
            tail_threshold=self.tail_threshold,
            tail_param = self.tail_param,
            tail_scale = self.tail_scale,
            norm_factor = self.norm_factor,
            y_input = tf.squeeze(self.y_input),
            dtype = self.dtype,
        )
        gpd_tail_prob_t = gpd_tail_prob(
            tail_threshold=self.tail_threshold,
            tail_param = self.tail_param,
            tail_scale = self.tail_scale,
            norm_factor = self.norm_factor,
            y_input = tf.squeeze(self.y_input),
            dtype = self.dtype,
        )

        # define final mixture probability tensors (from X and Y)
        self.pdf = mixture_prob(
            bool_split_tensor = bool_split_tensor,
            gpd_prob_t = gpd_prob_t,
            bulk_prob_t = bulk_prob_t,
            dtype = self.dtype,
        )
        
        self.log_pdf =  mixture_log_prob(
            bool_split_tensor = bool_split_tensor,
            gpd_prob_t = gpd_prob_t,
            bulk_prob_t = bulk_prob_t,
            dtype = self.dtype,
        )
        self.expanded_log_pdf = tf.expand_dims(self.log_pdf,axis=1)

        self.ecdf = tf.constant(1.00,dtype=self.dtype) - mixture_tail_prob(
            bool_split_tensor = bool_split_tensor,
            gpd_tail_prob_t = gpd_tail_prob_t,
            bulk_tail_prob_t = bulk_tail_prob_t,
            dtype = self.dtype,
        )

        # these models are used for probability predictions
        self.full_prob_model = keras.Model(
            inputs=[
                self.x_input,
                self.y_input
            ],
            outputs=[
                tf.cast(bool_split_tensor,dtype=self.dtype),
                tf.cast(tf.logical_not(bool_split_tensor),dtype=self.dtype),
                bulk_prob_t, 
                gpd_prob_t,
                tail_samples_count,
                bulk_samples_count,
            ],
            name="full_prob_model",
        )

        self._prob_pred_model = keras.Model(
            inputs=[
                self.x_input,
                self.y_input,
            ],
            outputs=[
                self.pdf,
                self.log_pdf,
                self.ecdf
            ],
            name="prob_pred_model",
        )

        self.norm_factor_model = keras.Model(
            inputs=self.x_input,
            outputs=[
                tf.expand_dims(self.norm_factor, axis=0), # very important "expand_dims"
            ],
            name="norm_factor_model",
        )

        # pipeline training model
        self._pl_training_model = keras.Model(
            inputs={**self.core_model.input_slices, 'y_input':self.y_input},
            outputs=[
                self.expanded_log_pdf, # in shape: (batch_size,1)
            ]
        )

        # normal training model
        self._training_model = keras.Model(
            inputs=[
                self.x_input,
                self.y_input,
            ],
            outputs=[
                self.expanded_log_pdf, # in shape: (batch_size,1)
            ]
        )

        # defne the loss function
        # y_pred will be self.log_pdf which is (batch_size,1)
        self._loss = lambda y_true, y_pred: -tf.reduce_sum(y_pred)

    @property
    def centers(self):
        return self._centers

    def bulk_mean(self,
        x : npt.NDArray[np.float64],
    ):

        prediction_res = self._params_model.predict(
            x,
        )
        result_dict = {}
        for idx,param in enumerate(self.params_config):
            result_dict[param] = np.squeeze(prediction_res[idx])

        # put mixture components together
        mixture_gamma_weights_t = tf.convert_to_tensor(result_dict['mixture_gamma_weights'], dtype=self.dtype)
        mixture_gamma_shapes_t = tf.convert_to_tensor(result_dict['mixture_gamma_shapes'], dtype=self.dtype)
        mixture_gamma_rates_t = tf.convert_to_tensor(result_dict['mixture_gamma_rates'], dtype=self.dtype)
        #tail_param_t = tf.convert_to_tensor(result_dict['tail_parameter'], dtype=self.dtype)
        #tail_threshold_t = tf.convert_to_tensor(result_dict['tail_threshold'], dtype=self.dtype)
        #tail_scale_t = tf.convert_to_tensor(result_dict['tail_scale'], dtype=self.dtype)

        # create gamma mixture
        cat = tfd.Categorical(probs=mixture_gamma_weights_t,dtype=self.dtype)
        components = [tfd.Gamma(concentration=shape, rate=rate) for shape, rate
                        in zip(tf.unstack(mixture_gamma_shapes_t, axis=1), tf.unstack(mixture_gamma_rates_t, axis=1))]
        mixture = tfd.Mixture(cat=cat, components=components)

        result = mixture.mean()
        return result.numpy()

    def quantile(self,
        x, # dict as below
        samples, # numbers between 0.0 and 1.0
    ):
        """
            vectorized numerical quantile finder using Scipy.optimize.newton
            https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.newton.html
        """
        # x = { 'queue_length1': np.zeros(1000), 'queue_length2': np.zeros(1000), 'queue_length3' : np.zeros(1000) }
        x_list = np.array([np.array([*items]) for items in zip(*x.values())])

        def model_cdf_fn(x ,a, b):
            pdf, logpdf, cdf = self.prob_batch(x=a,y=x)
            return cdf - b

        def model_pdf_fn(x, a, b=None):
            pdf, logpdf, cdf = self.prob_batch(x=a,y=x)
            return pdf

        result = optimize.newton(
            func = model_cdf_fn, 
            x0 = self.bulk_mean(x=x), # we feed the mean of the mixture as the initial guess
            args=(x_list,samples),
            fprime = model_pdf_fn,
            disp = True,
        )

        return result
    
    def sample_n(self, 
        x,
        rng : np.random.Generator = np.random.default_rng(seed=0),
    ):
        """
        https://github.com/tensorflow/probability/issues/659
        there is no closed form for the quantile of the Gaussian mixture
        In the last answer of these question, someone has mentioned that 
        Scipy can handle vectorized root finding:
        https://stackoverflow.com/questions/13088115/finding-the-roots-of-a-large-number-of-functions-with-one-variable
        This is what we use for quantile function.
        """
        # x = { 'queue_length1': np.zeros(1000), 'queue_length2': np.zeros(1000), 'queue_length3' : np.zeros(1000) }
        samples = rng.uniform(0.0,1.0,size = len(list(x.values())[0]))
        return self.quantile(x=x,samples=samples)