#!/usr/bin/env python

"""
This script adds series of simulated Gaussian events to the control light curves of a target SN.
We do this for different rolling sum kernel sizes ("sigma_kerns"). 
For each sigma_kern, we vary the peak magnitude ("peak_mag") and the kernel size ("sigma_sims") of the simulated event.

- Open the config file simulation_settings.json and edit as needed.
	- To only simulate events within observation seasons:
		- add MJD ranges of observation seasons as lists [STARTMJD, ENDMJD] to "observation_seasons"
		- ./simulate.py -m
	- To automatically calculate efficiencies using the FOM limits in the config file:
		- add detection limits to "sim_settings" for each sigma_kern object
		- ./simulate.py -e
- To load a config file with a different file name: ./simulate.py -f simulation_settings_2.json
- After script finishes running, open simulation_analysis.ipynb and load in light curves and saved tables to get a walkthrough analysis.
"""

from typing import Callable, Dict, List, Tuple
from abc import ABC, abstractmethod
import pandas as pd
import numpy as np
import sys, random, re, os, argparse, json
from copy import deepcopy
from scipy.interpolate import interp1d
from astropy.modeling.functional_models import Gaussian1D
from clean import hexstring_to_int
from download import make_dir_if_not_exists
from lightcurve import AveragedSupernova, AveragedLightCurve, AandB, AnotB, AorB
from pdastro import pdastrostatsclass

# TODO: fix to contain parameter columns
SIM_DETEC_TABLE_COLUMNS = [
	'sigma_kern', 
	'peak_appmag', 
	'peak_flux', 
	#'peak_mjd', 
	#'sigma_sim', 
	'control_index', 
	'max_fom', 
	'max_fom_mjd'
]

# TODO: fix to contain parameter columns
EFFICIENCY_TABLE_COLUMNS = [
	'sigma_kern', 
	'peak_appmag', 
	'peak_flux', 
	'sigma_sim'
]

"""
UTILITY
"""

# convert flux to magnitude 
def flux2mag(flux:float):
	return -2.5 * np.log10(flux) + 23.9

# convert magnitude to flux
def mag2flux(mag:float):
	return 10 ** ((mag - 23.9) / -2.5)

def get_valid_ix(table, colname, mjd_ranges, indices=None):
	if indices is None:
		indices = table.index.values
	valid_ix = []
	for i in indices:
		if in_valid_season(table.loc[i,colname], mjd_ranges):
			valid_ix.append(i)
	return valid_ix

# check if MJD is within valid MJD season
def in_valid_season(mjd, valid_seasons):
	in_season = False
	for season in valid_seasons:
		if mjd <= season[1] and mjd >= season[0]:
			in_season = True
	return in_season

# # generate list of peak fluxes and app mags
# def generate_peaks(peak_mag_min, peak_mag_max, n_peaks):
# 	peak_mags = list(np.linspace(peak_mag_min, peak_mag_max, num=n_peaks))
# 	peak_fluxes = list(map(mag2flux, peak_mags))

# 	peak_mags = [round(item, 2) for item in peak_mags]
# 	peak_fluxes = [round(item, 2) for item in peak_fluxes]

# 	return peak_mags, peak_fluxes

"""
ABSTRACT SIMULATION CLASS
"""

class Simulation(ABC):
	def __init__(self, name=None, **kwargs):
		"""
		Initialize the Simulation object.
		"""
		self.name = name
		self.peak_mjd = None

	@abstractmethod
	def get_sim_flux(self, mjds, peak_mjd=None, t0_mjd=None, **kwargs):
		"""
		Compute the simulated flux for given MJDs.

		:param mjds: List or array of MJDs.
		:param peak_mjd: MJD where the peak apparent magnitude should occur (optional).
		:param t0_mjd: MJD where the start of the model should occur (optional).

		:return: An array of flux values corresponding to the input MJDs.
		"""
		pass

	@abstractmethod
	def get_row_params(self):
		"""
		Construct a dictionary of parameters to add to a row in a SimDetecTable.
		Dictionary keys should function as column names and dictionary values 
		as column values.
		"""
		parameters = {
			'name': self.name,
			'peak_mjd': self.peak_mjd
		}
		return parameters

	def __str__(self):
		return f'Simulation of type {self.name}'

"""
ASYM GAUSSIAN
Adapted from A. Rest
"""

class Gaussian(Simulation):
	def __init__(self, sigma, peak_appmag, name='Gaussian', **kwargs):
		Simulation.__init__(self, name=name, **kwargs)
		self.peak_appmag = peak_appmag
		self.sigma = sigma
		self.g = self.new_gaussian(mag2flux(peak_appmag), sigma)

	def new_gaussian(self, peak_flux, sigma):
		x = np.arange(-100,100,.01)
		g1 = Gaussian1D(amplitude=peak_flux, stddev=sigma)(x)
		g2 = Gaussian1D(amplitude=peak_flux, stddev=sigma)(x)

		ind = np.argmin(abs(x))
		g3 = np.copy(g1)
		g3[ind:] = g2[ind:]
		gauss = np.array([x,g3])
		return gauss
	
	# get interpolated function of gaussian at peak MJD (peak_mjd) 
	# and match to time array (mjds)
	def get_sim_flux(self, mjds, peak_mjd=None, **kwargs):
		if peak_mjd is None:
			raise RuntimeError('ERROR: Peak MJD required to get flux of simulated Gaussian.')
		self.peak_mjd = peak_mjd

		g = deepcopy(self.g)
		g[0,:] += peak_mjd
		
		fn = interp1d(g[0],g[1],bounds_error=False,fill_value=0)
		sim_flux = fn(mjds)
		return sim_flux 
	
	def get_row_params(self):
		parameters = {
			'sigma_sim': self.sigma,
		}
		return parameters.update(super().get_row_params())
	
	def __str__(self):
		return f'Simulation of type {self.name} with peak app mag {self.peak_appmag:0.2f} and sigma_sim {self.sigma}'
	
"""
SIMULATED MODEL FROM LIGHT CURVE
"""

class Model(Simulation):
	def __init__(self, filename, peak_appmag=None, name='Pre-SN Outburst Model', **kwargs):
		Simulation.__init__(self, name=name, **kwargs)
		self.peak_appmag = peak_appmag
		#self.sigma = 2.8

		self.t = None 
		self.load(filename)

	def load(self, filename):
		print(f'Loading model light curve at {filename}...')

		try:
			self.t = pd.read_table(filename,delim_whitespace=True,header=None)
			self.t = self.t.rename(columns={0: "MJD", 1: "m"})
		except Exception as e:
			raise RuntimeError(f'ERROR: Could not load model at {filename}: {str(e)}')
	
		# calculate flux column
		self.t['uJy'] = self.t['m'].apply(lambda mag: mag2flux(mag))

	# get interpolated function of model at peak MJD (peak_mjd) 
	# and match to time array (mjds)
	def get_sim_flux(self, mjds, peak_mjd=None, peak_appmag=None, **kwargs):
		if peak_mjd is None:
			raise RuntimeError('ERROR: Peak MJD required to construct simulated model.')
		self.peak_mjd = peak_mjd
		
		# get original peak appmag index
		peak_idx = self.t['m'].idxmin() 
		
		if not peak_appmag is None:
			self.peak_appmag = peak_appmag
			
			# scale flux to the desired peak appmag
			self.t['uJy'] *= mag2flux(peak_appmag)/self.t.loc[peak_idx, 'uJy']

			# recalulate appmag column
			self.t['m'] = self.t['uJy'].apply(lambda flux: flux2mag(flux)) 
		else:
			self.peak_appmag = self.t.loc[peak_idx,'m']

		# put peak appmag at peak_mjd
		self.t['MJD'] -= self.t.loc[peak_idx,'MJD']
		self.t['MJD'] += peak_mjd 
		
		# interpolate lc and match to time array
		fn = interp1d(self.t['MJD'], self.t['uJy'], bounds_error=False, fill_value=0)
		sim_flux = fn(mjds)
		return sim_flux
	
	def get_row_params(self):
		# parameters = {
		# 	'sigma_sim': self.sigma, # TODO: is this necessary?
		# 	'peak_mjd': self.peak_mjd
		# }
		return super().get_row_params()
	
	def __str__(self):
		return f'Simulation of type {self.name} with peak app mag {self.peak_appmag:0.2f}'

class TessModel(Model):
	def __init__(self, filename, peak_appmag=None, name='TESS Pre-SN Outburst Model', **kwargs):
		Model.__init__(self, filename, name=name, peak_appmag=peak_appmag, **kwargs)
	
	def get_sim_flux(self, mjds, fn:interp1d):
		sim_flux = fn(mjds)
		return sim_flux

"""
ADD SIMULATIONS AND APPLY ROLLING SUM TO AVERAGED LIGHT CURVE 

first create table
go through row, using name or filename read params, use params to create object
sd can also be passed by user
for a loop, stick to one type of model
each parameter can be list/range/random
in config file have each possible model and their parameters, choose list/range/random for each parameter
now table can use config to know what params to expect

when loading file, pass desired MJD and mag or flux colnames

create sd table function in loop? can be overwritten by user
"""

class SimDetecSupernova(AveragedSupernova):
	def __init__(self, tnsname:str=None, mjdbinsize:float=1.0, filt:str='o'):
		AveragedSupernova.__init__(self, tnsname=tnsname, mjdbinsize=mjdbinsize, filt=filt)
		self.avg_lcs: Dict[int, SimDetecLightCurve] = {}

	def apply_rolling_sums(self, sigma_kern:float, flag=0x800000):
		for control_index in range(self.num_controls+1):
			self.avg_lcs[control_index].apply_rolling_sum(sigma_kern, flag=flag)

	def remove_rolling_sums(self):
		for control_index in range(self.num_controls+1):
			self.avg_lcs[control_index].remove_rolling_sum()

	def remove_simulations(self):
		for control_index in range(self.num_controls+1):
			self.avg_lcs[control_index].remove_simulations()

	def set_valid_seasons_ix(self, valid_seasons=None, mjd0=None, column='MJDbin'):
		for control_index in range(self.num_controls+1):
			self.avg_lcs[control_index].set_valid_seasons_ix(valid_seasons=valid_seasons, mjd0=mjd0, column=column)

	def load(self, input_dir, control_index=0):
		self.avg_lcs[control_index] = SimDetecLightCurve(control_index=control_index, filt=self.filt, mjdbinsize=self.mjdbinsize)
		if control_index == 0:
			filename = f'{input_dir}/{self.tnsname}.{self.filt}.{self.mjdbinsize:0.2f}days.lc.txt'
		else:
			filename = f'{input_dir}/controls/{self.tnsname}_i{control_index:03d}.{self.filt}.{self.mjdbinsize:0.2f}days.lc.txt'
		self.avg_lcs[control_index].load_lc_by_filename(filename)

class SimDetecLightCurve(AveragedLightCurve):
	def __init__(self, control_index=0, filt='o', mjd0=None, mjdbinsize=1.0, **kwargs):
		AveragedLightCurve.__init__(self, control_index, filt, mjdbinsize, **kwargs)
		self.cur_sigma_kern = None
		self.pre_mjd0_ix = self.ix_inrange('MJD', uplim=mjd0)
		self.valid_seasons_ix = None

	# get all light curve indices with MJDs within the given observation seasons
	def set_valid_seasons_ix(self, valid_seasons=None, column='MJDbin'):
		if valid_seasons is None:
			self.valid_seasons_ix = self.pre_mjd0_ix
		else:
			self.valid_seasons_ix = get_valid_ix(self.t, column, valid_seasons, indices=self.pre_mjd0_ix)
	
	# get a random MJD and its index that lies within the given observation seasons
	def get_rand_valid_mjd(self, valid_seasons=None, column='MJDbin'):
		if not valid_seasons is None:
			self.set_valid_seasons_ix(self, valid_seasons=valid_seasons, column=column)
		rand_mjd_idx = random.choice(self.valid_seasons_ix)
		rand_mjd = self.t.loc[rand_mjd_idx, 'MJDbin']
		return rand_mjd

	# remove rolling sum columns
	def remove_rolling_sum(self):
		self.cur_sigma_kern = None
		dropcols = []
		for col in ['__tmp_SN','SNR','SNRsum','SNRsumnorm']:
			if col in self.t.columns:
				dropcols.append(col)
		if len(dropcols) > 0:
			self.t.drop(columns=dropcols,inplace=True)
	
	# remove simulation columns
	def remove_simulations(self):
		dropcols = []
		for col in ['__tmp_SN','uJysim','SNRsim','simLC','SNRsimsum']:
			if col in self.t.columns:
				dropcols.append(col)
		if len(dropcols) > 0:
			self.t.drop(columns=dropcols,inplace=True)

	# apply a rolling sum to the light curve and add SNR, SNRsum, and SNRsumnorm columns
	def apply_rolling_sum(self, sigma_kern, indices=None, flag=0x800000, verbose=False):
		if indices is None:
			indices = self.getindices()
		if len(indices) < 1:
			raise RuntimeError('ERROR: not enough measurements to apply simulated gaussian')
		good_ix = AandB(indices, self.ix_unmasked('Mask', flag))

		self.remove_rolling_sum()
		self.cur_sigma_kern = sigma_kern
		self.t.loc[indices, 'SNR'] = 0.0
		self.t.loc[good_ix,'SNR'] = self.t.loc[good_ix,'uJy']/self.t.loc[good_ix,'duJy']

		new_gaussian_sigma = round(sigma_kern/self.mjdbinsize)
		windowsize = int(6 * new_gaussian_sigma)
		halfwindowsize = int(windowsize * 0.5) + 1
		if verbose:
			print(f'Sigma: {sigma_kern:0.2f} days; MJD bin size: {self.mjdbinsize:0.2f} days; sigma: {new_gaussian_sigma:0.2f} bins; window size: {windowsize} bins')

		# calculate the rolling SNR sum
		l = len(self.t.loc[indices])
		dataindices = np.array(range(l) + np.full(l, halfwindowsize))
		temp = pd.Series(np.zeros(l + 2*halfwindowsize), name='SNR', dtype=np.float64)
		temp[dataindices] = self.t.loc[indices,'SNR']
		SNRsum = temp.rolling(windowsize, center=True, win_type='gaussian').sum(std=new_gaussian_sigma)
		self.t.loc[indices,'SNRsum'] = list(SNRsum[dataindices])
		
		# normalize it
		norm_temp = pd.Series(np.zeros(l + 2*halfwindowsize), name='norm', dtype=np.float64)
		norm_temp[np.array(range(l) + np.full(l, halfwindowsize))] = np.ones(l)
		norm_temp_sum = norm_temp.rolling(windowsize, center=True, win_type='gaussian').sum(std=new_gaussian_sigma)
		self.t.loc[indices,'SNRsumnorm'] = list(SNRsum.loc[dataindices] / norm_temp_sum.loc[dataindices] * max(norm_temp_sum.loc[dataindices]))

	# add simulated flux to the light curve and add SNRsim and SNRsimsum columns
	def add_sim_flux(self, lc, good_ix, sim_flux, cur_sigma_kern=None, verbose=False):
		if cur_sigma_kern is None:
			cur_sigma_kern = self.cur_sigma_kern
		if cur_sigma_kern is None:
			raise RuntimeError('ERROR: No current sigma kern passed as argument or stored during previously applied rolling sum.')
		
		lc.t.loc[good_ix,'uJysim'] = lc.t.loc[good_ix,'uJy']
		lc.t.loc[good_ix,'uJysim'] += sim_flux 

		# make sure all bad rows have SNRsim = 0.0 so they have no impact on the rolling SNRsum
		lc.t['SNRsim'] = 0.0
		# include only simulated flux in the SNR
		lc.t.loc[good_ix,'SNRsim'] = lc.t.loc[good_ix,'uJysim']/lc.t.loc[good_ix,'duJy']

		new_gaussian_sigma = round(cur_sigma_kern/self.mjdbinsize)
		windowsize = int(6 * new_gaussian_sigma)
		halfwindowsize = int(windowsize * 0.5) + 1
		if verbose:
			print(f'Sigma: {cur_sigma_kern:0.2f} days; MJD bin size: {self.mjdbinsize:0.2f} days; new sigma: {new_gaussian_sigma:0.2f} bins; window size: {windowsize} bins')

		# calculate the rolling SNR sum for SNR with simulated flux
		l = len(self.t)
		dataindices = np.array(range(l) + np.full(l, halfwindowsize))
		temp = pd.Series(np.zeros(l + 2*halfwindowsize), name='SNRsim', dtype=np.float64)
		temp[dataindices] = lc.t['SNRsim']
		SNRsimsum = temp.rolling(windowsize, center=True, win_type='gaussian').sum(std=new_gaussian_sigma)
		lc.t['SNRsimsum'] = list(SNRsimsum.loc[dataindices])

		return lc

	# add a model to the light curve at a certain peak MJD and apparent magnitude
	def add_model(self, model:Model, peak_mjd, peak_appmag, cur_sigma_kern=None, flag=0x800000, verbose=False):
		if verbose:
			print(f'Adding simulated model: peak MJD = {peak_mjd:0.2f} MJD; peak app mag = {peak_appmag:0.2f}; sigma = {model.sigma:0.2f} days')

		lc = deepcopy(self)
		good_ix = AandB(lc.getindices(), lc.ix_unmasked('Mask', flag))
		sim_flux = model.get_sim_flux(lc.t.loc[good_ix,'MJD'], peak_mjd, peak_appmag)

		return self.add_sim_flux(lc, good_ix, sim_flux, cur_sigma_kern=cur_sigma_kern, verbose=verbose)

	# add a Gaussian bump to the light curve at a certain peak MJD
	def add_gaussian(self, gaussian:Gaussian, peak_mjd, cur_sigma_kern=None, flag=0x800000, verbose=False):
		if verbose:
			print(f'Adding simulated gaussian: peak MJD = {peak_mjd:0.2f} MJD; peak app mag = {gaussian.peak_appmag:0.2f}; sigma = {gaussian.sigma:0.2f} days')

		lc = deepcopy(self)
		good_ix = AandB(lc.getindices(), lc.ix_unmasked('Mask', flag))
		sim_flux = gaussian.get_sim_flux(lc.t.loc[good_ix,'MJD'], peak_mjd)
			
		return self.add_sim_flux(lc, good_ix, sim_flux, cur_sigma_kern=cur_sigma_kern, verbose=verbose)

	# add any simulation to the light curve, specifying parameters using keyword arguments
	def add_simulation(self, sim:Simulation, cur_sigma_kern=None, flag=0x800000, verbose=False, **kwargs):
		if verbose:
			print(f'Adding simulation: {sim}')
		
		lc = deepcopy(self)
		good_ix = AandB(lc.getindices(), lc.ix_unmasked('Mask', flag))
		sim_flux = sim.get_sim_flux(lc.t.loc[good_ix,'MJD'], **kwargs)
			
		return self.add_sim_flux(lc, good_ix, sim_flux, cur_sigma_kern=cur_sigma_kern, verbose=verbose)
	
	# get max FOM (for simulated FOM, column='SNRsimsum'; else column='SNRsumnorm') 
	# of measurements within the given indices
	def get_max_fom(self, indices=None, column='SNRsimsum'):
		if indices is None:
			indices = self.getindices()
		max_fom_idx = self.t.loc[indices, column].idxmax()

		max_fom_mjd = self.t.loc[max_fom_idx, 'MJDbin']
		max_fom = self.t.loc[max_fom_idx, column]
		return max_fom_mjd, max_fom

"""
SIMULATION DETECTION AND EFFICIENCY TABLES
"""

# get simulation detection dictionary (sd) key for a sigma_kern peak_appmag pair
def get_key(sigma_kern, peak_appmag=None, peak_flux=None):
	if peak_appmag is None and peak_flux is None:
		raise RuntimeError('ERROR: Cannot get the SimDetecTable key without peak app mag or flux.')
	if not peak_flux is None:
		peak_appmag = flux2mag(peak_flux)
	return f'{sigma_kern}_{peak_appmag:0.2f}'

# simulation detection table for a sigma_kern,peak_appmag pair
class SimDetecTable(pdastrostatsclass):
	def __init__(self, sigma_kern, num_iterations=None, peak_appmag=None, peak_flux=None, **kwargs):
		pdastrostatsclass.__init__(self, **kwargs)
		self.sigma_kern = sigma_kern
		
		if peak_appmag is None and peak_flux is None:
			raise RuntimeError('ERROR: Either peak app mag or flux required to construct SimDetecTable.')
		elif peak_appmag is None:
			peak_appmag = flux2mag(peak_flux)
		elif peak_flux is None:
			peak_flux = mag2flux(peak_appmag)
		self.peak_appmag = peak_appmag
		self.peak_flux = peak_flux

		if not num_iterations is None:
			self.setup(num_iterations)

	# set up a blank table and fill out the sigma_kern, peak_appmag, and peak_flux columns
	# (each column should be all the same value)
	def setup(self, num_iterations):
		self.t = pd.DataFrame(columns=SIM_DETEC_TABLE_COLUMNS)
		self.t['sigma_kern'] = np.full(num_iterations, self.sigma_kern)
		self.t['peak_appmag'] = np.full(num_iterations, self.peak_appmag)
		self.t['peak_flux'] = np.full(num_iterations, self.peak_flux)

	# update a certain row of the table
	# data should be structured like: data = {'column1': value1, 'column2': value2}
	def update_row_at_index(self, index, data:Dict):
		self.t.loc[index, data.keys()] = np.array(list(data.values()))

	def get_filename(self, tables_dir):
		key = get_key(self.sigma_kern, peak_appmag=self.peak_appmag)
		filename = f'{tables_dir}/simdetec_{key}.txt'
		return filename
	
	def load(self, tables_dir):
		filename = self.get_filename(tables_dir)
		try:
			self.load_spacesep(filename, delim_whitespace=True)
		except Exception as e:
			raise RuntimeError(f'ERROR: Could not load SimDetecTable at {filename}: {str(e)}')

	def save(self, tables_dir, overwrite=True):
		filename = self.get_filename(tables_dir)
		self.write(filename=filename, overwrite=overwrite)

	# get efficiency for a certain sigma_sim using the given FOM limit
	def get_efficiency(self, fom_limit, valid_seasons=None, column='sigma_sim', column_value=None):
		if column_value is None: 
			# get efficiency for all sigma_sims (entire table)
			column_ix = self.getindices()
		else:
			column_ix = self.ix_equal(column, column_value) #np.where(self.t['sigma_sim'] == sigma_sim)[0]
		if len(column_ix) < 1:
			print(f'WARNING: No {column} matches for {column_value}; returning NaN...')
			return np.nan
		
		if not valid_seasons is None:
			# get efficiency for simulations only occurring within mjd_ranges
			mjd_ix = get_valid_ix(self.t, 'peak_mjd', valid_seasons)
			column_ix = AandB(column_ix, mjd_ix)
		
		detected_ix = self.ix_inrange('max_fom', lowlim=fom_limit, indices=column_ix)
		efficiency = len(detected_ix)/len(column_ix) * 100
		return efficiency

class SimDetecTables:
	def __init__(self, sigma_kerns, num_iterations=None, peak_appmags=None, peak_fluxes=None):
		self.d:Dict[str, SimDetecTable] = None

		self.sigma_kerns = sigma_kerns

		if peak_appmags is None and peak_fluxes is None:
			raise RuntimeError('ERROR: either peak app mags or peak fluxes required to construct SimDetecTables.')
		elif peak_appmags is None:
			peak_appmags = list(map(flux2mag, peak_fluxes))
		elif peak_fluxes is None:
			peak_fluxes = list(map(mag2flux, peak_appmags))
		self.peak_appmags = peak_appmags
		self.peak_fluxes = peak_fluxes

		if not num_iterations is None:
			self.setup(num_iterations)

	# set up a dictionary with the key as f'{sigma_kern}_{peak_appmag:0.2f}' 
	# and the value as the corresponding SimDetecTable
	def setup(self, num_iterations):
		self.d = {}
		for sigma_kern in self.sigma_kerns:
			for peak_appmag in self.peak_appmags:
				key = get_key(sigma_kern, peak_appmag=peak_appmag)
				self.d[key] = SimDetecTable(sigma_kern, 
																		peak_appmag=peak_appmag, 
																		num_iterations=num_iterations)
	
	# update the row of a SimDetecTable at a certain index
	def update_row_at_index(self, sigma_kern, peak_appmag, index, data):
		key = get_key(sigma_kern, peak_appmag=peak_appmag)
		self.d[key].update_row_at_index(index, data)
				
	# get efficiency for a certain SimDetecTable and sigma_sim using the given FOM limit
	def get_efficiency(self, sigma_kern, peak_appmag, fom_limit, valid_seasons=None, column_name='sigma_sim', column_value=None):
		key = get_key(sigma_kern, peak_appmag=peak_appmag)
		return self.d[key].get_efficiency(fom_limit, valid_seasons=valid_seasons, column_name=column_name, column_value=column_value)
	
	def load_all(self, tables_dir):
		self.d = {}
		for sigma_kern in self.sigma_kerns:
			for peak_appmag in self.peak_appmags:
				key = get_key(sigma_kern, peak_appmag=peak_appmag)
				self.d[key] = SimDetecTable(sigma_kern, peak_appmag=peak_appmag)
				self.d[key].load(tables_dir)

	def save(self, tables_dir, sigma_kern, peak_appmag):
		key = get_key(sigma_kern, peak_appmag=peak_appmag)
		self.d[key].save(tables_dir)

	def save_all(self, tables_dir):
		for table in self.d.values():
			table.save(tables_dir)

class EfficiencyTable(pdastrostatsclass):
	def __init__(self, sigma_kerns, sigma_sims, peak_appmags=None, peak_fluxes=None, fom_limits=None, **kwargs):
		pdastrostatsclass.__init__(self, **kwargs)

		self.sigma_kerns = sigma_kerns
		self.set_sigma_sims(sigma_sims)
		self.set_fom_limits(fom_limits)

		if peak_appmags is None and peak_fluxes is None:
			raise RuntimeError('ERROR: either peak app mags or peak fluxes required to construct an EfficiencyTable.')
		elif peak_appmags is None:
			peak_appmags = list(map(flux2mag, peak_fluxes))
		elif peak_fluxes is None:
			peak_fluxes = list(map(mag2flux, peak_appmags))
		self.peak_appmags = peak_appmags
		self.peak_fluxes = peak_fluxes

		self.setup()

	# set up a blank EfficiencyTable and fill out sigma_kern, peak_appmag, and peak_flux columns
	def setup(self):
		self.t = pd.DataFrame(columns=['sigma_kern', 'peak_appmag', 'peak_flux', 'sigma_sim'])

		for sigma_kern in self.sigma_kerns: 
			n = len(self.peak_appmags) * len(self.sigma_sims[sigma_kern])
			
			df = pd.DataFrame(columns=['sigma_kern', 'peak_appmag', 'peak_flux', 'sigma_sim'])
			df['sigma_kern'] = np.full(n, sigma_kern)
			df['peak_appmag'] = np.repeat(self.peak_appmags, len(self.sigma_sims[sigma_kern]))
			df['peak_flux'] = np.repeat(self.peak_fluxes, len(self.sigma_sims[sigma_kern]))
			
			j = 0
			while(j < n):
				for sigma_sim in self.sigma_sims[sigma_kern]:
					df.loc[j, 'sigma_sim'] = sigma_sim
					j += 1
			self.t = pd.concat([self.t, df], ignore_index=True)

	# create dictionary of sigma_sims, with sigma_kerns as the keys
	def set_sigma_sims(self, sigma_sims):	
		if isinstance(sigma_sims, List):
			self.sigma_sims = {}
			for i in range(len(self.sigma_kerns)):
				self.sigma_sims[self.sigma_kerns[i]] = sigma_sims[i]
		else:
			if len(sigma_sims) != len(self.sigma_kerns):
				raise RuntimeError('ERROR: Each entry in sigma_kerns must have a matching list in sigma_sims')
			self.sigma_sims = sigma_sims

	# create dictionary of FOM limits, with sigma_kerns as the keys
	def set_fom_limits(self, fom_limits):
		if fom_limits is None:
			return
				
		if isinstance(fom_limits, list):
			self.fom_limits = {}
			for i in range(len(self.sigma_kerns)):
				self.fom_limits[self.sigma_kerns[i]] = fom_limits[i]
		else:
			if len(fom_limits) != len(self.sigma_kerns):
				raise RuntimeError('ERROR: Each entry in sigma_kerns must have a matching list in fom_limits')
			self.fom_limits = fom_limits

	# remove previously calculated efficiency columns
	def reset_table(self):
		for col in self.t.columns:
			if re.search('^pct_detec_',col):
				self.t.drop(col, axis=1, inplace=True)

		for i in range(len(self.sigma_kerns)):
			fom_limits = self.fom_limits[self.sigma_kerns[i]]
			for fom_limit in fom_limits:
				self.t[f'pct_detec_{fom_limit:0.2f}'] = np.full(len(self.t), np.nan)
	
	# calculate efficiencies using SimDetecTables and update EfficiencyTable
	def get_efficiencies(self, sd:SimDetecTables, valid_seasons=None, column_name='sigma_sim', column_value=None, verbose=False):
		if self.fom_limits is None:
			raise RuntimeError('ERROR: fom_limits is None')
		
		for i in range(len(self.t)):
			sigma_kern = self.t.loc[i,'sigma_kern']
			peak_appmag = self.t.loc[i,'peak_appmag']
			sigma_sim = self.t.loc[i,'sigma_sim']

			if verbose:
				print(f'Getting efficiencies for sigma_kern {sigma_kern}, sigma_sim {sigma_sim}, peak_mag {peak_appmag}...')

			for fom_limit in self.fom_limits[sigma_kern]:
				self.t.loc[i,f'pct_detec_{fom_limit:0.2f}'] = \
					sd.get_efficiency(sigma_kern, 
											 			peak_appmag,
														fom_limit, 
														valid_seasons=valid_seasons,
														column_name=column_name,
														column_value=column_value)
	
	# get a subset of the EfficiencyTable
	def get_subset(self, sigma_kern=None, fom_limit=None, sigma_sim=None):
		colnames = EFFICIENCY_TABLE_COLUMNS
		
		if sigma_kern is None:
			ix = self.getindices()
		else:
			ix = self.ix_equal('sigma_kern', sigma_kern) 

		if not sigma_sim is None: 
			ix = self.ix_equal('sigma_sim', sigma_sim, indices=ix)

		if not fom_limit is None:
			try: 
				colnames.append(f'pct_detec_{fom_limit:0.2f}')
			except Exception as e:
				raise RuntimeError(f'ERROR: no matching FOM limit {fom_limit}: {str(e)}')
		else:
			for col in self.t.columns:
				if re.search('^pct_detec_',col):
					colnames.append(col)
		
		return self.t.loc[ix,colnames]
	
	# merge with other EfficiencyTable
	def merge_tables(self, other):
		if not isinstance(other, EfficiencyTable):
			raise RuntimeError(f'ERROR: Cannot merge EfficiencyTable with object type: {type(other)}')
		
		self.sigma_kerns += other.sigma_kerns
		self.sigma_sims += other.sigma_sims
		if not self.fom_limits is None:
			self.fom_limits.update(other.fom_limits)

		self.t = pd.concat([self.t, other.t], ignore_index=True)

	def load(self, tables_dir, filename='efficiencies.txt'):
		print(f'Loading efficiency table {filename}...')
		filename = f'{tables_dir}/{filename}'
		try:
			#self.t = pd.read_table(filename, delim_whitespace=True)
			self.load_spacesep(filename, delim_whitespace=True)
		except Exception as e:
			raise RuntimeError(f'ERROR: Could not load efficiency table at {filename}: {str(e)}')
	
	def save(self, tables_dir, filename='efficiencies.txt'):
		print(f'Saving efficiency table {filename}...')
		filename = f'{tables_dir}/{filename}'
		#self.t.to_string(filename, index=False)
		self.write(filename=filename, overwrite=True, index=False)
	
	def __str__(self):
		return self.t.to_string()
	
"""
GENERATE AND SAVE SIMULATED DETECTION AND EFFICIENCY TABLES
"""

# define command line arguments
def define_args(parser=None, usage=None, conflict_handler='resolve'):
	if parser is None:
		parser = argparse.ArgumentParser(usage=usage, conflict_handler=conflict_handler)
		
	parser.add_argument('-f', '--config_file', default='simulation_settings.json', type=str, help='file name of JSON file with settings for this class')
	parser.add_argument('-e', '--efficiencies', default=False, action='store_true', help='calculate efficiencies using FOM limits')
	parser.add_argument('-m', '--obs_seasons', default=False, action='store_true', help='only simulate events with peak MJDs within the observation season MJD ranges')
	parser.add_argument('--mjd0', type=float, default=None, help='transient start date in MJD')

	return parser

# load the JSON config file
def load_json_config(config_file):
		try:
			print(f'Loading config file at {config_file}...')
			with open(config_file) as cfg:
				return json.load(cfg)
		except Exception as e:
			raise RuntimeError(f'ERROR: Could not load config file at {config_file}: {str(e)}')

# TODO: documentation
class SimDetecLoop(ABC):
	def __init__(self, output_dir:str, tables_dir:str, sigma_kerns:List, **kwargs):
		self.output_dir = output_dir
		self.tables_dir = tables_dir
		self.sigma_kerns = sigma_kerns

		self.sn:SimDetecSupernova = None
		self.e:EfficiencyTable = None
		self.sd:SimDetecTables = None

	@abstractmethod
	def set_peak_mags_and_fluxes(self, peak_mag_min:float=23.0, peak_mag_max:float=16.0, n_peaks:int=20, **kwargs):
		self.peak_appmags = list(np.linspace(peak_mag_min, peak_mag_max, num=n_peaks))
		self.peak_fluxes = list(map(mag2flux, self.peak_appmags))
	
	@abstractmethod
	def load_sn(self, tnsname, num_controls, valid_seasons=None, mjd0=None, mjdbinsize=1.0, filt='c'):
		self.sn.load_all(output_dir, num_controls=num_controls)
		self.sn.remove_rolling_sums()
		self.sn.remove_simulations()
		self.sn.set_valid_seasons_ix(valid_seasons=valid_seasons)

	@abstractmethod
	def modify_light_curve(self, control_index=0):
		pass

	@abstractmethod
	def generate_random_simulation(self, **kwargs) -> Simulation:
		pass
	
	@abstractmethod
	def generate_peak_mjd(self, control_index):
		peak_mjd = self.sn.avg_lcs[control_index].get_rand_valid_mjd()
		return peak_mjd

	@abstractmethod
	def get_max_fom(self, sim_lc:SimDetecLightCurve, **kwargs) -> Tuple[float, float]:
		pass

	@abstractmethod
	def add_row_to_sd(self, sigma_kern, peak_appmag, index, sim:Simulation, control_index, max_fom, max_fom_mjd):
		sim_params = sim.get_row_params()
		other_params = {
			'control_index': control_index,
			'max_fom': max_fom,
			'max_fom_mjd': max_fom_mjd
		}
		row = other_params.update(sim_params)
		self.sd.update_row_at_index(sigma_kern, peak_appmag, index, row)

	@abstractmethod
	def calculate_efficiencies(self, valid_seasons=None):
		pass

	@abstractmethod
	def loop(self, num_iterations=50000):
		pass

class AtlasSimDetecLoop(SimDetecLoop):
	def __init__(self, 
							 output_dir:str, 
							 tables_dir:str, 
							 sigma_kerns:List, 
							 sigma_sims):
		SimDetecLoop.__init__(output_dir, tables_dir, sigma_kerns)
		self.sigma_sims = sigma_sims

	def load_sn(self, tnsname, num_controls, valid_seasons=None, mjd0=None, mjdbinsize=1, filt='c'):
		if mjd0 is None:
			print('WARNING: No MJD0 provided. Using entire MJD range for simulations.')
		super().load_sn(tnsname, num_controls, valid_seasons, mjd0, mjdbinsize, filt)

	def generate_random_simulation(self, peak_appmag=None, **kwargs):
		if peak_appmag is None:
			raise RuntimeError('ERROR: A peak apparent magnitude is required to generate a Gaussian simulation.')
		# pick random sigma_sim
		sigma_sim = random.choice(self.sigma_sims)
		# construct Gaussian object with random sigma_sim and given peak_appmag
		return Gaussian(sigma_sim, peak_appmag)
	
	def get_max_fom(self, sim_lc:SimDetecLightCurve, peak_mjd=None, sigma_sim=None, **kwargs):
		if peak_mjd is None or sigma_sim is None:
			raise RuntimeError('ERROR: A peak MJD and sigma of the simulated Gaussian is required to find the max FOM.')
		# measurements within 1 sigma of the peak MJD
		indices = sim_lc.ix_inrange(colnames='MJDbin', lowlim=peak_mjd-sigma_sim, uplim=peak_mjd+sigma_sim)
		# get max FOM of those measurements
		max_fom_mjd, max_fom = sim_lc.get_max_fom(indices=indices)
		return max_fom_mjd, max_fom

	def calculate_efficiencies(self, fom_limits=None, valid_seasons=None):
		self.e:EfficiencyTable = EfficiencyTable(self.sigma_kerns, 
																					 	 self.sigma_sims, 
																						 peak_appmags=self.peak_appmags, 
																						 peak_fluxes=self.peak_fluxes, 
																						 fom_limits=fom_limits)
		print(f'\nCalculating efficiencies...')
		# calculate efficiencies using FOM limits from config file
		self.e.get_efficiencies(self.sd, valid_seasons=valid_seasons)
		print(self.e.t.to_string())
		self.e.save(self.tables_dir)
	
	def loop(self, flag=0x800000, num_iterations=50000):
		self.sd:SimDetecTables = SimDetecTables(sigma_kerns, 
																						num_iterations=num_iterations, 
																						peak_appmags=self.peak_appmags, 
																						peak_fluxes=self.peak_fluxes)
		
		# loop through each rolling sum kernel
		for sigma_kern in self.sigma_kerns:
			print(f'\nUsing rolling sum kernel size sigma_kern={sigma_kern} days...')
			self.sn.apply_rolling_sums(sigma_kern)
			
			# loop through each possible peak apparent magnitude
			for peak_index in range(len(self.peak_appmags)):
				peak_appmag = self.peak_appmags[peak_index]
				peak_flux = self.peak_fluxes[peak_index]
				
				print(f'Commencing {num_iterations} iterations for peak app mag {peak_appmag} (peak flux {peak_flux})...')
				for j in range(num_iterations):
					# pick random control light curve
					rand_control_index = random.choice(valid_control_ix)

					# generate random simulated Gaussian
					gaussian = self.generate_random_simulation(peak_appmag=peak_appmag)

					# generate random peak MJD within valid observation seasons
					peak_mjd = self.generate_peak_mjd()

					# add simulated Gaussian to the control light curve
					sim_lc = self.sn.avg_lcs[rand_control_index].add_gaussian(gaussian, peak_mjd, flag=flag)

					# get max simulated FOM within 1 sigma of the peak MJD
					max_fom_mjd, max_fom = self.get_max_fom(sim_lc, peak_mjd=peak_mjd, sigma_sim=gaussian.sigma)
					
					self.add_row_to_sd(sigma_kern, 
														 peak_appmag, 
														 j, 
														 gaussian, 
														 rand_control_index, 
														 max_fom, 
														 max_fom_mjd)
					
				# save SimDetecTable
				self.sd.save(self.tables_dir, sigma_kern, peak_appmag)
		
		print('\nSuccess')

class TessSimDetecLoop(SimDetecLoop):
	def __init__(self, 
							 output_dir:str, 
							 tables_dir:str, 
							 sigma_kerns:List):
		pass

if __name__ == "__main__":
	args = define_args().parse_args()
	config = load_json_config(args.config_file)

	output_dir = config["data_dir"]
	tables_dir = config["tables_dir"]
	make_dir_if_not_exists(tables_dir)
	print(f'\nData directory containing SN and control light curves: {output_dir}')
	print(f'Directory to store tables in: {tables_dir}')
	print(f'\nEfficiency calculation: {args.efficiencies}')
	num_iterations = int(config["num_iterations"])
	print(f'\nNumber of simulated events per peak magnitude: {num_iterations}')
	badday_flag = config['sn_settings']['badday_flag']
	print(f'\nBad day cut (averaging) flag: {hex(badday_flag)}')

	if args.obs_seasons:
		if len(config["observation_seasons"]) < 1:
			raise RuntimeError("ERROR: Please fill out observation seasons in the config file before using the -m argument.")
		valid_seasons = config["observation_seasons"]
		print(f'\nSimulating events only within the following observation seasons: \n{valid_seasons}')
	else:
		valid_seasons = None
	if not args.mjd0 is None:
		print(f'Simulating events only before MJD0: {args.mjd0} MJDO')
	
	num_controls = int(config["sn_settings"]["num_controls"])
	valid_control_ix = [i for i in range(1, num_controls+1) if not i in config["skip_control_ix"]]
	print(f'\nSimulating events only within the following control light curves: \n', valid_control_ix)

	sigma_kerns = []
	sigma_sims = {}
	fom_limits = None
	if args.efficiencies:
		fom_limits = {}
	for obj in config['sim_settings']:
		sigma_kerns.append(obj['sigma_kern'])
		sigma_sims[obj['sigma_kern']] = obj['sigma_sims']
		if args.efficiencies:
			if  len(obj['fom_limits']) < 1:
				raise RuntimeError(f'ERROR: Efficiency calculation set to {args.efficiencies}, but no FOM limits provided for sigma_kern={obj["sigma_kern"]}.')
			fom_limits[obj['sigma_kern']] = obj['fom_limits']
	print(f'\nRolling sum kernel sizes (sigma_kerns): \n{sigma_kerns}')
	print(f'\nSimulated event kernel sizes (sigma_sims) for each sigma_kern: \n{sigma_sims}')
	if args.efficiencies:
		print(f'\nFOM limits for each sigma_kern: \n{fom_limits}')

	simdetec = SimDetecLoop(output_dir, tables_dir, sigma_kerns, sigma_sims)
	simdetec.set_peak_mags_and_fluxes()
	simdetec.load_sn(config["sn_settings"]["tnsname"], 
									 num_controls, 
									 valid_seasons=valid_seasons,
									 mjd0=args.mjd0,
									 mjdbinsize=config["sn_settings"]["mjd_bin_size"],
									 filt=config["sn_settings"]["filt"])
	simdetec.loop(num_iterations=num_iterations, flag=badday_flag)
	if args.efficiencies:
		simdetec.calculate_efficiencies(fom_limits=fom_limits, 
																		valid_seasons=valid_seasons)