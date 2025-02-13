
"""
Functions for evaluating and visualizing M/EEG source estimates.
Author: John GW Samuelsson. 
"""

import numpy as np
#from mayavi import mlab
from multiprocessing import Pool

import mne
from mne.minimum_norm import read_inverse_operator, prepare_inverse_operator
from mne.minimum_norm.inverse import _assemble_kernel
from mne.io import RawArray

from .source_space_tools import remove_overlap_in_labels, blurring
from .mne_simulation import get_raw_noise
from .settings import settings_class
from .plotting_tools import plot_topographic_parcellation


def setup_labels(subject, parc, subjects_dir, unwanted_labels=['unknown', 'corpuscallosum']):
    """Returns a list of labels from parcellation, removing unwanted labels.

    Parameters
    ----------
    subject : string
    parc : string
        The name of the parcellation file located in labels dir of the subjects_dir.
    subjects_dir : string      
    unwanted_labels : list
        List of strings, containing the names of the unwanted labels.

    Returns
    -------
    labels : list
        The list of the labels.
    unwanted_labels : list
        The list of the unwanted labels.

    """
    #Read all labels
    all_labels = mne.read_labels_from_annot(subject, parc, subjects_dir=subjects_dir)
    labels = []
    labels_unwanted = []
    for label in all_labels:
        if label.name[0:-3] not in unwanted_labels:
            labels.append(label)
        else:
            labels_unwanted.append(label)
            print('Removing ' + label.name)
    
    # Reorder labels in terms of hemispheres
    labels_lh = [label for label in labels if label.hemi=='lh']
    labels_rh = [label for label in labels if label.hemi=='rh']
    labels = labels_lh + labels_rh
    
    return labels, labels_unwanted


def setup(subjects_dir, subject, data_path, fname_raw, fname_fwd,
          fname_eve, fname_trans, fname_epochs, n_epochs, meg_and_eeg, plot_labels=False):
    """
    Setup some important subject data.
    """
    #Save data paths in settings object
    settings = settings_class(
                 subjects_dir=subjects_dir,
                 subject=subject,
                 data_path=data_path, 
                 fname_raw=fname_raw,
                 fname_fwd=fname_fwd,
                 fname_eve=fname_eve,
                 fname_trans=fname_trans,
                 fname_epochs=fname_epochs,
                 meg_and_eeg=meg_and_eeg)
    
    #Read all labels
    all_labels = mne.read_labels_from_annot(settings.subject(), 'laus500', subjects_dir=settings.subjects_dir())
    labels = []
    labels_unwanted = []
    for label in all_labels:
        if 'unknown' not in label.name and 'corpuscallosum' not in label.name:
            labels.append(label)
        else:
            labels_unwanted.append(label)
            print('Removing ' + label.name)
    
    # Reorder labels in terms of hemispheres
    labels_lh = [label for label in labels if label.hemi=='lh']
    labels_rh = [label for label in labels if label.hemi=='rh']
    labels = labels_lh + labels_rh
    
    # Load forward matrix
    fwd = mne.read_forward_solution(settings.fname_fwd())
    fwd = mne.convert_forward_solution(fwd, surf_ori=True, force_fixed=True)

    # Remove sources in unwanted labels (corpus callosum and unknown)
    sources_to_remove = np.array([])
    offset = fwd['src'][0]['nuse']
    vertnos_lh = fwd['src'][0]['vertno']
    vertnos_rh = fwd['src'][1]['vertno']
    for label in labels_unwanted:
        vertnos = label.vertices
        
        # Find vertices to remove from the gain matrix
        if label.hemi == 'lh':
            sources_to_remove = np.concatenate((sources_to_remove, np.where(np.in1d(vertnos_lh, vertnos))[0]))
            src_ind= 0
        if label.hemi == 'rh':
            sources_to_remove = np.concatenate((sources_to_remove, np.where(np.in1d(vertnos_rh, vertnos))[0] + offset))
            src_ind = 1
            
        # Correct src info
        fwd['src'][src_ind]['inuse'][vertnos] = 0
        fwd['src'][src_ind]['nuse'] = np.sum(fwd['src'][src_ind]['inuse'])
        fwd['src'][src_ind]['vertno'] = np.nonzero(fwd['src'][src_ind]['inuse'])[0]   
    source_to_keep = np.where(~np.in1d(range(fwd['sol']['data'].shape[1]), sources_to_remove.astype(int)))[0]
    fwd['sol']['data'] = fwd['sol']['data'][:,source_to_keep]
    fwd['nsource'] = fwd['src'][0]['nuse'] + fwd['src'][1]['nuse']
    
    # Plot labels
    if plot_labels:
        from surfer import Brain
        brain = Brain(subject_id=subject, hemi='lh', surf='inflated', subjects_dir=subjects_dir,
              cortex='low_contrast', background='white', size=(800, 600))
        brain.add_annotation('laus500')
    
    # Get epochs (n_epochs randomly picked from all available) from resting state recordings
    epochs = mne.read_epochs(settings.fname_epochs())
    epochs_to_use = np.random.choice(len(epochs), size=n_epochs, replace=False)
    
    return settings, labels, labels_unwanted, fwd, epochs_to_use


def _getInversionKernel(inverse_operator, nave=1, lambda2=1. / 9., method='MNE', label=None, pick_ori=None):
    """Returns the inversion kernel of the inverse_operator.

    Parameters
    ----------
    inverse_operator : InverseOperator
        Instance of inverse operator to extract inverse kernel from.
    nave : int
        Number of averages (scales the noise covariance).
    lambda2 : float
        The regularization factor. Recommended to be 1 / SNR**2.
    method : "MNE" | "dSPM" | "sLORETA" | "eLORETA"
        Use minimum norm, dSPM (default), sLORETA, or eLORETA.
    label : Label | None
        Restricts the source estimates to a given label. If None,
        source estimates will be computed for the entire source space.
    pick_ori : None | "normal" | "vector"
        Which orientation to pick (only matters in the case of 'normal').

    Returns
    -------
    K : array, shape (n_vertices, n_channels) | (3 * n_vertices, n_channels)
        The kernel matrix. 

    """

    inv = prepare_inverse_operator(inverse_operator,nave,lambda2,method)
    K = _assemble_kernel(inv,label,method,pick_ori)[0]
    return K


def _convert_real_resolution_matrix_to_labels(R, labels, label_verts):
    """Converts a point-source resolution matrix to a patch-source resolution
    matrix based on labels by summing all rows in the point-source matrix 
    corresponding to sources in each label, mimicking simultaneous activation 
    of all sources in the patch. The average of the absolute values of 
    reconstructed sources in each patch is then taken as the patch activity
    reconstruction.

    Parameters
    ----------
    R : array, shape (n_vertices, n_vertices)
        Point-source resolution matrix.
    labels : list
        List of labels representing patches.
    label_verts : dictionary
        Dictionary containing label names as keys and their vertix indicies as 
        values.

    Returns
    -------
    R_label : array, shape (n_labels, n_labels) 
        The patch-source resolution matrix. 

    """
    R_label = np.zeros((len(labels),len(labels)))
    for a, label in enumerate(labels):
        for b, label_r in enumerate(labels):
            R_label[b,a] = np.mean(np.abs(np.sum(R[:,label_verts[label.name]],axis=1)[label_verts[label_r.name]]))
#            R_label[b,a] = np.mean(np.sum(R[label_verts[label_r.name],:][:,label_verts[label.name]], axis=1))
    return R_label 


def standardize_columns(R, arg):
    """Standardizes matrix R with respect to the maximal value in each column 
    or its diagonal value.

    Parameters
    ----------
    R : array
        Matrix to be normalized.
    arg : "diag" | "max"
        String indicating whether matrix' columns should be normalized with respect
        to its diagonal or maximal values.

    Returns
    -------
    R_std : array
        Normalized matrix. 

    """
    if arg not in ['diag','max']:
        raise ValueError('arg must be either "diag" or "max". Breaking.')
    R_std = R.copy()
    for d in range(len(R_std.T)):
        if arg == 'diag':
            diag = R_std[d,d]
            R_std[:,d] = R_std[:,d]/diag
        if arg == 'max':
            R_std[:,d] = R_std[:,d]/np.max(R_std[:,d])
    return R_std


def get_noise(epochs, epochs_to_use):
    """Returns average of epochs corresponding to epochs_to_use and noise 
    covariance computed from the other epochs.

    Parameters
    ----------
    epochs : instance of Epochs
    epochs_to_use : list
        List containing indicies of epochs to use to calculate average.

    Returns
    -------
    noise_cov : instance of Covariance
        Noise covariance.
    epochs_ave : instance of Evoked
        Average of epochs.
    """

    epochs.pick_types(meg=True, eeg=True, exclude=[])
    epochs.pick_types(meg=True, eeg=True)
    noise_epochs = epochs[[x for x in range(len(epochs)) if x not in epochs_to_use]]
    noise_cov = mne.compute_covariance(noise_epochs)
    
    epochs = epochs[epochs_to_use]
    epochs_ave = epochs.average()
    
    return noise_cov, epochs_ave


def get_empirical_R_large(data_path, subjects, inv_methods, SNRs, waveform, inv_function):
    """
    Get empirical resolution matrix for large simulations (splits up inverse
    methods in different runs)
    """
    
    nested_inv_methods = [[inv_method] for inv_method in inv_methods]
    for inv_methods_n in nested_inv_methods:
        R_emp = evaler.get_empirical_R(data_path, subjects, inv_methods_n, SNRs, waveform, inv_function, len(SNRs))
        pickle.dump(R_emp, open('./R_emp_'+inv_methods[0],'wb'))

    return 

def load_R_emp(inv_methods):
    """Loads empirical resolution matrices computed and saved for different 
    inverse methods.

    Parameters
    ----------
    inv_methods : list
        List containing strings of names of inverse methods.

    Returns
    -------
    R_emp_all : dictionary
        Loaded empirical resolution matrix containing data for all inverse methods.
    """

    R_emp = pickle.load(open('./R_emp_'+inv_methods[0],'rb'))
    R_emp_all = {}
    for subj in list(R_emp.keys()):
        R_emp_all.update({subj : {}})
        for stats in list(R_emp[subj].keys()):
            R_emp_all[subj].update({stats : {}})
            if stats in ['r_master', 'r_master_point_patch']:
                for inv_method in inv_methods:
                    R_emp_all[subj][stats].update({inv_method : []})
            else:
                for sub_stats in list(R_emp[subj][stats].keys())[0:2]:
                    R_emp_all[subj][stats].update({sub_stats : {}})
                    for inv_method in inv_methods:
                        R_emp_all[subj][stats][sub_stats].update({inv_method : []})
    
    for inv_method in inv_methods:
        R_emp = pickle.load(open('./R_emp_'+inv_method,'rb'))
        for subj in list(R_emp.keys()):
            for stats in list(R_emp[subj].keys()):
                if stats in ['r_master', 'r_master_point_patch']:
                    R_emp_all[subj][stats][inv_method] = R_emp[subj][stats][inv_method]
                else:
                    for sub_stats in list(R_emp[subj][stats].keys())[0:2]:
                        R_emp_all[subj][stats][sub_stats][inv_method] = R_emp[subj][stats][sub_stats][inv_method]
    R_emp_all[list(R_emp_all.keys())[0]]['r_master'].update({'SNRs' : SNRs})
    
    return R_emp_all


def correct_fwd(fwd, labels_unwanted):
    """Corrects a forward object by removing the parts of the gain matrix 
    corresponding to sources in labels_unwanted.

    Parameters
    ----------
    fwd : instance of Forward object
    labels_unwanted : list
        List containing the unwanted labels.

    Returns
    -------
    fwd : instance of Forward
        The corrected forward object.
    """
    sources_to_remove = np.array([])
    offset = fwd['src'][0]['nuse']
    vertnos_lh = fwd['src'][0]['vertno']
    vertnos_rh = fwd['src'][1]['vertno']
    for label in labels_unwanted:
        vertnos = label.vertices
        
        # Find vertices to remove from the gain matrix
        if label.hemi == 'lh':
            sources_to_remove = np.concatenate((sources_to_remove, np.where(np.in1d(vertnos_lh, vertnos))[0]))
            src_ind= 0
        if label.hemi == 'rh':
            sources_to_remove = np.concatenate((sources_to_remove, np.where(np.in1d(vertnos_rh, vertnos))[0] + offset))
            src_ind = 1
            
        # Correct src info
        fwd['src'][src_ind]['inuse'][vertnos] = 0
        fwd['src'][src_ind]['nuse'] = np.sum(fwd['src'][src_ind]['inuse'])
        fwd['src'][src_ind]['vertno'] = np.nonzero(fwd['src'][src_ind]['inuse'])[0]   
    source_to_keep = np.where(~np.in1d(range(fwd['sol']['data'].shape[1]), sources_to_remove.astype(int)))[0]
    fwd['sol']['data'] = fwd['sol']['data'][:,source_to_keep]
    fwd['nsource'] = fwd['sol']['data'].shape[1]
    
    return fwd
 

def get_R(inp):
    """Calculates empirical and/or analytical resolution matrix.

    Parameters
    ----------
    inp : tuple containing the following parameters;
        waveform : array
            Activation waveform; time signal of label activation amplitude.
        fname_fwd : string
            Path to forward object.
        labels : list
            List containing parcellation labels.
        invmethod : string
            Inverse method to test.    
            Could be "MNE" | "dSPM" | "sLORETA" | "eLORETA" | "mixed_norm"
            for MNE, dSPM, sLORETA, eLORETA or MxNE source estimate, respectively.
            If invmethod is none of these, then you have to give your own estimate
            in inv_function.            
        labels_unwanted : list
            List containing unwanted labels.
        SNR : float
            SNR in sensor space of the estimation, how the simulated signal will
            be scaled.
        activation_labels : list
            List of labels to be activated. Defaults to labels argument.
        compute_analytical : Boolean
            If True, analytical resolution matrix will be computed as well. Only
            works if invmethod is MNE, dSPM, sLORETA or eLORETA.
        inv_function : function handle
            User-defined function that returns the estimate. See documentation in
            for_manuscript.py.
        cov_fname : string
            Path to noise covariance. Will be used in creating the inverse operator.
        ave_fname : string
            Path to average Evoked object, will act as background activity to be
            superposed with simulated signal.

    Returns
    -------
    If compute_analytical is True:
        R_emp : array, shape (n_labels, n_activation_labels)
            Empirical resolution matrix.
        R_analytical : array, shape (n_labels, n_labels)
            Analytical resolution matrix grouped together in labels.
        R : array, shape (n_vertices, n_vertices)
            Analytical resolution matrix for point sources.
    If compute_analytical is False:
        R_emp : array, shape (n_labels, n_activation_labels)
            Empirical resolution matrix.
        R_label_vert : array, shape (n_vertices, n_activation_labels)
            Empirical resolution matrix, but where the resulting estimates are 
            not grouped into labels but remains as point sources.
    """
    import warnings
    waveform, fname_fwd, labels, invmethod, labels_unwanted, SNR, \
        activation_labels, compute_analytical, inv_function, cov_fname, ave_fname  = inp

    epochs_ave = mne.read_evokeds(ave_fname)[0]
    noise_cov = mne.read_cov(cov_fname)
    fwd = mne.read_forward_solution(fname_fwd)
    fwd = mne.convert_forward_solution(fwd, surf_ori=True, force_fixed=True)
    fwd = correct_fwd(fwd, labels_unwanted)

    if activation_labels == None:
        activation_labels=labels
        
    # SNR has to be inf if we are to compute closed form R - otherwise different activations will be scaled differently
    if compute_analytical:
        SNR = np.inf
    
    # Create a dictionary linking labels with its vertices
    label_verts = {}
    for label in labels:
        if label.hemi == 'lh':
            hemi_ind = 0
            vert_offset = 0
        if label.hemi == 'rh':
            hemi_ind = 1     
            vert_offset = fwd['src'][0]['nuse']
        verts_in_src_space = label.vertices[np.isin(label.vertices,fwd['src'][hemi_ind]['vertno'])]
        inds = np.where(np.in1d(fwd['src'][hemi_ind]['vertno'],verts_in_src_space))[0]+vert_offset
        if len(inds) == 0:
            warnings.warn(label.name + ' label has no active source.')
        label_verts.update({label.name : inds})
        
############## Noise from epochs
    goodies = np.array([c for c, ch in enumerate(epochs_ave.info['ch_names']) if not ch in epochs_ave.info['bads']])
    noise = epochs_ave.data[:, 0:waveform.shape[1]]

    R_emp = np.zeros((len(labels),len(activation_labels)))
    R_label_vert = np.zeros((fwd['src'][0]['nuse']+fwd['src'][1]['nuse'], len(activation_labels)))

    # Find noise power for mags, grads and eeg (will be used for SNR scaling later)
    mod_ind = {'mag' : [], 'grad' : [], 'eeg' : []}
    noise_power = mod_ind.copy()
    start_ind = 0
    for c, (meg, eeg) in enumerate([('mag', False), ('grad', False), (False, True)]):
        inds = mne.pick_types(epochs_ave.info, meg = meg, eeg = eeg)
        mod_ind[list(mod_ind.keys())[c]] = inds
        noise_power[list(mod_ind.keys())[c]] = np.mean(np.linalg.norm(noise[inds, :], axis=1))
    noise = noise[goodies, :]
    
    # Create inverse operator if applied inverse method is linear
    if invmethod in ['MNE', 'dSPM', 'eLORETA', 'sLORETA']:
        
        inverse_operator = mne.minimum_norm.make_inverse_operator(epochs_ave.info, fwd, noise_cov, depth=None, fixed=True)
    else:
        inverse_operator = None

    # Find activated source amplitude scaling by setting averge SNR to constant
    sign_pow = {}
    for key in list(mod_ind.keys()):
        signal_powers = []
        for c,label in enumerate(activation_labels):
            inds = label_verts[label.name]
            G = fwd['sol']['data'][:, :][:, inds]*10**-8
            signal = np.sum(G, axis=1)
            signal_power = np.mean(np.abs(signal[mod_ind[key]])*np.sqrt(waveform.shape[1]))
            signal_powers.append(signal_power)
        sign_pow.update({key : np.mean(signal_powers)})
    snrs = [sign_pow[key] / noise_power[key] for key in list(mod_ind.keys())]
    ave_scale = SNR / np.mean(snrs)


    # Patch activation of each label
    for c,label in enumerate(activation_labels):
        inds = label_verts[label.name]
        G = fwd['sol']['data'][:, inds]
        act = np.repeat(waveform, repeats=len(inds), axis=0)*10**-8
        signal = np.dot(G,act)

        """
        Find average empirical SNR and invert to get average scaled SNR of grads, mags and EEG right. 
        Note that we take the average of the absolute value of the resolution matricx at each time point in order 
        to avoid problems with baseline correction. Therefore we should not scale the SNR with respect to the number 
        of time points in the waveform. If calculating time-averaged resolution matrix for constant activation function, 
        also scale SNR with respect to number of time points.
        """
### Fixed SNR scaling for each activation starts here
#        empirical_SNR = 0
#        for key in list(mod_ind.keys()):
#            signal_strength = np.mean(np.linalg.norm(signal[mod_ind[key], :], axis=1))
#            empirical_SNR = empirical_SNR + 1. / 3. * signal_strength / noise_power[key]
#        scaling = SNR / empirical_SNR
### Fixed scaling ends here
        signal = signal[goodies, :]
        scaling = ave_scale
        if scaling == np.inf:
            sens = signal
            lambda2 = 1./9.
        else:
            sens = scaling * signal + noise 
        
        evoked = epochs_ave.copy()
        evoked.data = sens
        evoked.set_eeg_reference('average', projection=True, verbose='WARNING')
        # Need to put evoked into format with bad channels for inverse modeling
        evoked_mod = evoked.copy()
        evoked_mod._data = np.zeros((len(evoked.ch_names), evoked._data.shape[1]))
        evoked_mod._data[:] = np.nan
        evoked_mod._data[goodies, :] = evoked._data
        evoked = evoked_mod

        if invmethod == 'mixed_norm':
            estimate = mne.inverse_sparse.mixed_norm(evoked, fwd, noise_cov, alpha = 55, 
                                                        loose = 0, verbose='WARNING')
            lh_verts = estimate.vertices[0]
            rh_verts = estimate.vertices[1]
            src_inds = np.where(np.in1d(fwd['src'][0]['vertno'],lh_verts))[0]
            src_inds = np.concatenate((src_inds, np.where(np.in1d(fwd['src'][1]['vertno'],rh_verts))[0]+fwd['src'][0]['nuse']))
            
            for d,label_r in enumerate(labels):
                inds_r = label_verts[label_r.name]
                dipoles_in_label = np.where(np.isin(src_inds, inds_r))[0]
                # Let resolution matrix of the entry be the mean of that entry over all time chunks
                if dipoles_in_label.shape[0] > 0:
                    R_emp[d,c] = np.mean(np.abs(estimate.data[dipoles_in_label,:]))
            R_label_vert[src_inds,c] = np.mean(np.abs(estimate.data), axis=1)
            if np.sum(R_emp[:,c]) == 0.:
                break_point
                raise Exception('Estimate was equal to zero. Aborting.')
        else:

            # Inv_function is a user specified inverse method that maps evoked -> array (n_labels x n_labels)
            source = inv_function(evoked, SNR, invmethod, inverse_operator)
            if np.sum(np.isnan(source))>0:
                break_point
                raise Exception('Nan value found in source estimate. Aborting.')
            # Loop through each label and populate entries in resolution matrix with the mean of the absolute over time.       
            for d,label_r in enumerate(labels):
                inds_r = label_verts[label_r.name]
                # Each entry is the average of all dipole amplitudes in the patch over time
                R_emp[d,c] = np.mean(np.abs(source[inds_r,:]))
            # Resolution matrix without summing of patch vertices, resulting in shape (n_vertices, n_labels)
            R_label_vert[:,c] = np.mean(np.abs(source), axis=1)
            
        print(labels[0].subject + ', ' + invmethod + ': ' + str(c/len(activation_labels) * 100) + ' %... ', end='\r', flush=True)
    print('\n done.')

    if compute_analytical:
        
        inv = mne.minimum_norm.prepare_inverse_operator(inverse_operator, nave=1, lambda2=lambda2, method=invmethod)
        if invmethod == 'MNE':
            K = mne.minimum_norm.inverse._assemble_kernel(inv,label=None,method=invmethod,pick_ori=None)[0]
        elif invmethod == 'dSPM' or invmethod == 'sLORETA':
            inv_matrices = mne.minimum_norm.inverse._assemble_kernel(inv,label=None,method=invmethod,pick_ori=None)
            K = np.dot(np.diag(inv_matrices[1].flatten()), inv_matrices[0])
        else:
            print('Resolution matrix on closed form is only available for linear methods; MNE, dSPM and sLORETA. Returning only empirical...')
            return R_emp
        R = np.dot(K, fwd['sol']['data'][goodies, :])
        R_analytical = _convert_real_resolution_matrix_to_labels(R, labels, label_verts)
        return R_emp, R_analytical, np.abs(R)
    
    else:
        return (np.abs(R_emp), R_label_vert)


def remove_diagonal(A):
    """
    Removes diagonal from matrix A.
    """
    
    A_off_diagonal = A.copy()
    np.fill_diagonal(A_off_diagonal, 9999999999999999)
    bool_ind = A_off_diagonal.T==9999999999999999
    return A_off_diagonal.T[np.where(~bool_ind)].reshape(A_off_diagonal.shape[1], A_off_diagonal.shape[0]-1).T  


def get_average_cross_talk_map(R):
    """Returns median of standardized rows (cross-talk).

    Parameters
    ----------
    R : array, shape (n_sources, n_sources)
        Resolution matrix.
    Returns
    -------
    acm : array, shape (n_sources)
        Median cross-talk of each activation.
    """
    R = standardize_rows(R)
    acm = np.median(R,axis=1)
    return acm


def get_average_point_spread(R,arg='max'):
    """Returns median of standardized columns (point-spread).

    Parameters
    ----------
    R : array, shape (n_sources, n_sources)
        Resolution matrix.
    arg : 'max' | 'diag'
        String indicating whether matrix' columns should be normalized with respect
        to its diagonal or maximal values.
    Returns
    -------
    acm : array, shape (n_sources)
        Median point-spread of each activation.
    """
    R = standardize_columns(R,arg)
    R_copy = remove_diagonal(R)
    acm = np.median(R_copy,axis=0)
    return acm


def get_spatial_dispersion(R, src, labels):
    """Calculates spatial dispersion (a metric for point spread) as defined in 
    Samuelsson et al. 2020.

    Parameters
    ----------
    R : array, shape (n_sources, n_sources)
        Resolution matrix.
    src : List of Source objects
        Source space of the subject.
    labels : list, containing n_sources elements
        List containing the parcellation labels. 

    Returns
    -------
    SD : array, shape (n_sources)
        Spatail dispersion in centimetres.
    """
    rr = np.concatenate((src[0]['rr'][src[0]['vertno']],
                         src[1]['rr'][src[1]['vertno']]), axis=0)
    peak_positons = rr[np.argmax(np.abs(R), axis=0), :]
#    #Hauk
#    dist = np.linalg.norm(np.repeat(rr.reshape(1, len(rr), 3), repeats=rr.shape[0], axis=0)
#                          - np.repeat(rr.reshape(rr.shape[0], 1, 3), repeats=len(rr), axis=1), axis=2)
#    SD_hauk = np.sqrt(np.divide(np.diag(np.dot(dist.T, R**2)), np.sum(R**2, axis=0)))*10
#    #Molins
#    dist = np.linalg.norm(np.repeat(rr.reshape(1, len(rr), 3), repeats=rr.shape[0], axis=0)
#                          - np.repeat(rr.reshape(rr.shape[0], 1, 3), repeats=len(rr), axis=1), axis=2)
#    SD_molins = np.sqrt(np.divide(np.diag(np.dot(dist.T**2, R**2)), np.sum(R**2, axis=0)))*100    
    #Samuelsson
    dist = np.linalg.norm(np.repeat(peak_positons.reshape(1, len(peak_positons), 3), repeats=R.shape[0], axis=0)
                          - np.repeat(rr.reshape(rr.shape[0], 1, 3), repeats=len(peak_positons), axis=1), axis=2)
    SD = np.divide(np.diag(np.dot(dist.T, R)), np.sum(np.abs(R), axis=0))*100
#    r_dist = np.linalg.norm(np.repeat(rr.reshape((rr.shape[0], rr.shape[1], 1)), repeats=peak_positons.shape[0], axis=2) - \
#                       np.repeat(peak_positons.reshape((peak_positons.shape[0], peak_positons.shape[1], 1)), repeats=rr.shape[0], axis=2).T, axis=1)
#    mean_dist = np.mean(r_dist, axis=0)
#    SD = np.divide(SD, mean_dist)
    return SD
    

def get_spherical_coge(R, settings, labels):
    """Calculates center of gravity and error over the spherical surface manifold,
    each hemisphere separately.

    Parameters
    ----------
    R : array, shape (n_sources, n_sources)
        Resolution matrix.
    settings: Settings object
        Instance of settings containing subject details.
    labels : list
        List containing the parcellation labels.

    Returns
    -------
    spherical_coge : array, shape (n_sources)
        Center of gravity error over spherical surface.
    """
    src = mne.setup_source_space(subject=settings.subject(), surface='sphere', spacing='ico5',
                                 subjects_dir=settings.subjects_dir(), add_dist=False)

    labels_divide = [[(c, label) for c, label in enumerate(labels) if label.hemi=='lh'],
                 [(c, label) for c, label in enumerate(labels) if label.hemi=='rh']]
    source_ind_hemi = [np.array([label[0] for label in labels_divide[0]]),
                       np.array([label[0] for label in labels_divide[1]])]

    spherical_coge = {'coge' : [], 'cog_vector_norm' : []}
    for c, hemi in enumerate(['lh', 'rh']):
        src_sphere = src[c]
        
        # Get vertices, move center of sphere to origo
        rr = src_sphere['rr'] - np.mean(src_sphere['rr'], axis=0)
        radius = np.mean(np.linalg.norm(rr, axis=1)) 
        
        # Get hemispherical resolution matrix and label centers
        R_hemi = R[source_ind_hemi[c],:][:, source_ind_hemi[c]]
        labels_hemi = labels_divide[c]
        label_center = []
        for label in labels_hemi:
            verts = label[1].vertices
            label_center.append(np.mean(rr[verts,:],axis=0))
        label_center = np.array(label_center)             

        # Calculate center of gravity and error on spherical surface
        for c, col in enumerate(R_hemi.T):
            center_of_gravity = np.dot(col, label_center)/np.sum(col)
            center_of_gravity = center_of_gravity*radius/np.linalg.norm(center_of_gravity)
            error = np.linalg.norm(label_center[c] - center_of_gravity)
            surface_error = 2 * radius * np.arcsin(error / (2 * radius))
            spherical_coge['cog_vector_norm'].append(np.linalg.norm(center_of_gravity) / radius)
            spherical_coge['coge'].append(surface_error)
            
    spherical_coge['cog_vector_norm'] = np.array(spherical_coge['cog_vector_norm'])
    spherical_coge['coge'] = 100*np.array(spherical_coge['coge'])    
    return spherical_coge

def get_label_center_points(labels, src, src_space_sphere):
    """Finds the center vertex of each label in labels. The center vertex is
    defined as the closest vertex to the center of gravity of all vertices in
    each label in the spherical source space.

    Parameters
    ----------
    labels : list
        List of labels.
    src : list
        List of surface source space objects.
    src_space_sphere : list
        List of spherical surface source space objects.

    Returns
    -------
    center_points : array, shape (n_sources, 3)
        Positions of center sources in each label.
    center_vertices : array, shape (n_sources)
        Indicies of center vertices.
    """
    labels_divide = [[(c, label) for c, label in enumerate(labels) if label.hemi=='lh'],
                 [(c, label) for c, label in enumerate(labels) if label.hemi=='rh']]
    
    center_points = []
    center_vertices = []
    zero_labels = []
    for c, hemi in enumerate(['lh', 'rh']):
        src_sphere = src_space_sphere[c]
        
        # Get vertices, move center of sphere to origo
        rr = src_sphere['rr'] - np.mean(src_sphere['rr'], axis=0)
        
        # Get hemispherical resolution matrix and label centers
        labels_hemi = labels_divide[c]
        for label in labels_hemi:
            label_verts = label[1].vertices
            label_center = np.mean(rr[label_verts,:],axis=0)
            sources_in_label = np.array([vert for vert in label[1].vertices if vert in src[c]['vertno']])
            if len(sources_in_label) == 0:
                zero_labels.append(label)
            else:
                center_vertex = sources_in_label[np.argmin(np.linalg.norm(label_center-rr[sources_in_label, :], axis=1))]
                center_vertices.append(center_vertex + c*src[0]['np'])

    print('zero labels:')
    print(zero_labels)

    if len(zero_labels) > 0:
        return np.zeros((1000,3)), np.zeros((1000,3))
    center_vertices = np.array(center_vertices)
    center_points = np.concatenate((src[0]['rr'], src[1]['rr']), axis=0)[center_vertices, :]
    
    return center_points, center_vertices 


def get_peak_dipole_error(R_vl, src, src_space_sphere, labels):
    """Calculates localization bias in the source estimates as the distance 
    between the center point of the activated label and the location of the 
    reconstructed source with the highest amplitude.

    Parameters
    ----------
    R_vl : array, shape (n_vertices, n_labels)
        Resolution matrix where source reconstructions have not been grouped
        together based on labels.
    src : list
        List of surface source space objects.
    src_space_sphere : list
        List of surface spherical source space objects.
    labels : list
        List of labels.

    Returns
    -------
    errors : array, shape (n_labels)
        Peak localization errors.
    """
    
    label_center_points = get_label_center_points(labels, src, src_space_sphere)[0]
    max_sources = np.argmax(np.abs(R_vl), axis=0)
    rr = np.concatenate((src[0]['rr'][src[0]['vertno']], 
                         src[1]['rr'][src[1]['vertno']]), axis=0)
    errors = np.linalg.norm(rr[max_sources, :] - label_center_points, axis=1)*100
    
    return errors

        

def get_label_center(labels, src):
    """Get center of gravity of sources of each label in labels.

    Parameters
    ----------
    src : list
        List of source space objects.
    labels : list
        List containing the labels.

    Returns
    -------
    label_center : array, shape (n_labels, 3)
        The center of gravity of each label.
    """
    label_center = []

    for label in labels:
        verts = label.vertices
        if label.hemi=='lh':
            label_center.append(np.mean(src[0]['rr'][verts,:],axis=0))
        if label.hemi=='rh':
            label_center.append(np.mean(src[1]['rr'][verts,:],axis=0))
        
    label_center = np.array(label_center)
    
    return label_center

    
def get_center_of_gravity_error(R, src, labels):     
    """Calculates the center of gravity error.

    Parameters
    ----------
    R : array, shape (n_labels, n_labels)
        Resolution matrix.
    src : list
        List of source space objects.
    labels : list
        List containing the labels.

    Returns
    -------
    coge : array, shape (n_labels)
        Center of gravity errors in centimetres.
    center_of_gravity_list : list
        List containing the center of gravity of the labels.
    cog_closest_source : list
        List of the closest sources to the center of gravity of each label.
        
    """
    label_center = get_label_center(labels, src)            
    coge = []
    center_of_gravity_list = []
    cog_closest_source = []

    for c, col in enumerate(R.T):
        center_of_gravity = np.dot(col, label_center)/np.sum(col)
        error = np.linalg.norm(label_center[c] - center_of_gravity)
        coge.append(error)
        center_of_gravity_list.append(center_of_gravity)
        cog_closest_source.append(np.argmin(np.linalg.norm(np.repeat(center_of_gravity.reshape((1,3)),
                                                                     len(labels),axis=0)-label_center, axis=1)))
        
    return 100*np.array(coge), center_of_gravity_list, cog_closest_source


def resolution_map(settings, R, res_argument, arg='max', fpath='', print_surf=False):
    """Displays and optionally prints a topographic map of cross talk and point spread.

    Parameters
    ----------
    settings : instance of Settings object
    R : array, shape (n_labels, n_labels)
        Resolution matrix.
    res_argument : 'point_spread' | 'cross_talk'
        Which resolution metrics to plot.
    arg : 'max' | 'diag'
        Which element to standardize the resolution matrix with. If 'diag',
        then resolution matrix will be standardized with respect to the activated
        source and cross-talk and point-spread could be above 1. If 'max', then
        resolution matrix is standardized with respect to the maximal value in
        each column and row, respectively, and cross-talk and point-spread is 
        between 0 to 1.
    fpath : string
        File path and name to print ply file if print_surf is True.
    print_surf : boolean
        If True, will print a ply file with the resolution maps to the file 
        specified by fpath.

    Returns
    -------
    acm : array, shape (n_labels)
        Point spread or cross talk for each activation.
    brain : plot
        mlab surface plot object.
    """
    if res_argument not in ['point_spread','cross_talk']:
        raise ValueError('res_argument has to be either point_spread or cross_talk')
    if res_argument == 'point_spread':
        acm = get_average_point_spread(R, arg)
    if res_argument == 'cross_talk':
        acm = get_average_cross_talk_map(R)
    
    src = mne.read_forward_solution(settings.fname_fwd())['src']
    src_joined = join_source_spaces(src)
    scalars = blurring(acm, src_joined)
    brain = mlab.triangular_mesh(src_joined['rr'][:, 0], src_joined['rr'][:, 1], src_joined['rr'][:, 2], src_joined['tris'], scalars = scalars)
    if print_surf:
        if len(fpath) == 0:
            raise ValueError('Must provide fpath to where plyfile will be printed.')
        else:
            print_ply(fpath, src_joined, scalars, vmax = True, vmin = True)
    return acm, brain 


def get_r_master(SNRs, waveform, fname_fwd, labels, inv_methods, labels_unwanted,
                 cov_fname, ave_fname, activation_labels=None, inv_function=None, n_jobs=1):
    """Wrapper that calls get_R to calculate empirical resolution matrices.

    Parameters
    ----------
    SNRs : list
        List of SNR values to get empirical resolution matrix for.
    waveform : array
        Activation waveform; time signal of label activation amplitude.
    fname_fwd : string
        Path to forward object.
    labels : list
        List containing parcellation labels.
    inv_methods : list
        Inverse methods to test, elements could be:    
        "MNE" | "dSPM" | "sLORETA" | "eLORETA" | "mixed_norm".
        for MNE, dSPM, sLORETA, eLORETA or MxNE source estimate, respectively.
        If invmethod is none of these, then you have to give your own estimate
        in inv_function.            
    labels_unwanted : list
        List containing unwanted labels.
    cov_fname : string
        Path to noise covariance. Will be used in creating the inverse operator.
    ave_fname : string
        Path to average Evoked object, will act as background activity to be
        superposed with simulated signal.
    activation_labels : list
        List of labels to be activated. Defaults to labels argument.
    inv_function : function handle
        User-defined function that returns the estimate. See documentation in
        for_manuscript.py.
    n_jobs : int
        Number of threads to run in parallell.

    Returns
    -------
    r_master: dictionary
        Dictionary with SNR values as keys and empirical resolution matrices
        of shape (n_labels, n_labels) as values.
    r_master_vl: dictionary
        Dictionary with SNR values as keys and empirical resolution matrices
        of shape (n_vertices, n_labels), where vertices have not been grouped
        into labels, as values.
    """    
    if  n_jobs < 2*len(SNRs):
        SNR_njobs = n_jobs
        activation_jobs = 1
    else:
        SNR_njobs = len(SNRs)
        activation_jobs = np.floor_divide(n_jobs, len(SNRs))
    compute_analytical = False
    r_master = {}
    r_master_vl = {}
    iterations = np.floor_divide(len(SNRs), SNR_njobs)
    N_remainder = np.mod(len(SNRs), SNR_njobs)
    fwd = mne.read_forward_solution(fname_fwd)
    fwd = mne.convert_forward_solution(fwd, surf_ori=True, force_fixed=True)
    fwd = correct_fwd(fwd, labels_unwanted)
    vertno = fwd['src'][0]['nuse'] + fwd['src'][1]['nuse']
    
    SNR_iterations = [SNRs[iteration*SNR_njobs : (iteration + 1) * SNR_njobs] for iteration in range(0, iterations)]
    if not N_remainder == 0:
        SNR_iterations.append(SNRs[SNR_njobs*iterations:len(SNRs)])

    for inv_method in inv_methods:
        if activation_labels == None:
            r_tensor = np.zeros((len(labels), len(labels), len(SNRs)))
            r_tensor_vl = np.zeros((vertno, len(labels), len(SNRs)))
            activation_labels = labels
        else:
            r_tensor = np.zeros((len(labels), len(activation_labels), len(SNRs)))
            r_tensor_vl = np.zeros((vertno, len(activation_labels), len(SNRs)))
        print('Computing resolution matrices for inverse method ' + inv_method + '...')
        for group_count, SNR_group in enumerate(SNR_iterations):
            from joblib import Parallel, delayed
            myfunc = delayed(get_R)
            parallel = Parallel(n_jobs=SNR_njobs*activation_jobs)
            activation_chunks = [list(np.array_split(np.array(activation_labels),activation_jobs)[i]) 
                                            for i in range(activation_jobs)]
            inp_group = []
            for SNR in SNR_group:
                for k in range(activation_jobs):
                    inp_group.append((SNR,activation_chunks[k]))

            out = parallel(myfunc((waveform, fname_fwd, labels, inv_method, labels_unwanted,
                                   inp[0], inp[1], compute_analytical, inv_function, 
                                   cov_fname, ave_fname)) for inp in inp_group)

            for c, SNR in enumerate(SNR_group):
                R_emp = np.array([]).reshape(len(labels),0)
                R_emp_vl = np.array([]).reshape(vertno,0)
                for d, activation_chunk in enumerate(activation_chunks):
                    R_emp = np.concatenate((R_emp, out[c*activation_jobs+d][0]), axis=1)
                    R_emp_vl = np.concatenate((R_emp_vl, out[c*activation_jobs+d][1]), axis=1)
                r_tensor[:, :, c + group_count*len(SNR_group)] = R_emp
                r_tensor_vl[:, :, c + group_count*len(SNR_group)] = R_emp_vl
        r_master.update({inv_method : r_tensor})
        r_master_vl.update({inv_method : r_tensor_vl})
    
    r_master_vl.update({'SNRs' : SNRs})
    r_master.update({'SNRs' : SNRs})
    return r_master, r_master_vl


def get_roc_statistics(r_master, inv_methods):
    """Gets ROC statistics from r_master object.

    Parameters
    ----------
    r_master : dictionary
        r_master object, of the type returned by get_r_master function.
    inv_methods : list
        List of strings of inverse methods to calculate ROC for.

    Returns
    -------
    roc_stats : dictionary
        Dictionary containing ROC stats roc, auc and all_stats.
    """
    roc_stats = {'roc' : {}, 'acu' : {}, 'all_stats' : {}}
    
    for inv_method in inv_methods:
        R_tensor = r_master[inv_method]
        roc_list = {'roc' : [], 'acu' : [], 'all_stats' : []}
        
        for c,SNR in enumerate(list(r_master['SNRs'])):
            roc, acu, all_stats = get_roc(R_tensor[:,:,c])
            roc_list['roc'].append(roc)
            roc_list['acu'].append(acu)
            roc_list['all_stats'].append(all_stats)            
            
        roc_stats['roc'].update({inv_method : roc_list['roc']})
        roc_stats['acu'].update({inv_method : roc_list['acu']})
        roc_stats['all_stats'].update({inv_method : roc_list['all_stats']})
        
    return roc_stats


def get_prc_statistics(r_master, inv_methods):
    """Gets PRC statistics from r_master object.

    Parameters
    ----------
    r_master : dictionary
        r_master object, of the type returned by get_r_master function.
    inv_methods : list
        List of strings of inverse methods to calculate ROC for.

    Returns
    -------
    prc_stats : dictionary
        Dictionary containing PRC stats prc, auc and all_stats.
    """
    prc_stats = {'prc' : {}, 'acu' : {}, 'all_stats' : {}}
    
    for inv_method in inv_methods:
        R_tensor = r_master[inv_method]
        prc_list = {'prc' : [], 'acu' : [], 'all_stats' : []}
        
        for c,SNR in enumerate(list(r_master['SNRs'])):
            prc, acu = get_prc(R_tensor[:,:,c])
            prc_list['prc'].append(prc)
            prc_list['acu'].append(acu)
            
        prc_stats['prc'].update({inv_method : prc_list['prc']})
        prc_stats['acu'].update({inv_method : prc_list['acu']})
        
    return prc_stats


def get_roc(R):
    """Calculates ROC curve from resolution matrix R.

    Parameters
    ----------
    R : array, shape (n_sources, n_sources)
        Resolution matrix.

    Returns
    -------
    ROC : array, shape (2, n_T)
        Points on the ROC curve, i.e., (False Positive Rate, True Positive Rate).
        The threshold value will vary from -0.01 to 1.01 with n_T as the number
        of points in between (can be an arbitrary value).
    acu : float
        Area under the ROC curve. Will be between 0 and 1.
    all_stats : dictionary
        Dictionary with True positives, False negatives, True negatives and 
        False positives as keys and their respective values for each threshold 
        value.
    """
    n_T = 100
    ROC = np.zeros((2,n_T+3))    
    R_max = standardize_columns(R, arg='max')
    true_estimates = np.diag(R_max)
    R_max_off_diagonals = remove_diagonal(R_max)
    acu = []
    all_stats = {'TP' : [], 'FN' : [], 'TN' : [], 'FP' : []}

    for c,T in enumerate(np.linspace(-0.01,1.01,n_T)):
        TP = np.sum(true_estimates > T)
        FN = len(true_estimates) - TP
        TN = np.sum(R_max_off_diagonals <= T)
        FP = np.sum(R_max_off_diagonals >= T)
        TPR = TP/(TP+FN)
        FPR = FP/(FP+TN)
        ROC[0,c] = FPR
        ROC[1,c] = TPR
        all_stats['TP'].append(TP)
        all_stats['FN'].append(FN)
        all_stats['TN'].append(TN)
        all_stats['FP'].append(FP)

    for stats in list(all_stats.keys()):
        all_stats[stats] = np.array(all_stats[stats])
    
    acu = np.abs(np.trapz(y=ROC[1,:], x=ROC[0,:], dx=0.001))
    return ROC, acu, all_stats


def get_prc(R):
    """Calculates precision-recall curve from resolution matrix R.
    Y-axis: PPV = TP/(TP+FP)
    X-axis: TPR = TP/(TP+FN)

    Parameters
    ----------
    R : array, shape (n_sources, n_sources)
        Resolution matrix.

    Returns
    -------
    ROC : array, shape (2, n_T)
        Points on the PRC curve, i.e., (False Positive Rate, True Positive Rate).
        The threshold value will vary from -0.01 to 1.01 with n_T as the number
        of points in between (can be an arbitrary value).
    acu : float
        Area under the ROC curve. Will be between 0 and 1.
    """
    n_T = 1000
    PRC = np.zeros((2,n_T))#np.zeros((2,n_T+3))    
    R_max = standardize_columns(R, arg='max')
    true_estimates = np.diag(R_max)
    R_max_off_diagonals = remove_diagonal(R_max)
    acu = []

    for c,T in enumerate(np.linspace(-0.001,1.001,n_T)):
        TP = np.sum(true_estimates > T)
        FN = len(true_estimates) - TP
        TN = np.sum(R_max_off_diagonals <= T)
        FP = np.sum(R_max_off_diagonals >= T)
        PPV = TP/(TP+FP)
        TPR = TP/(TP+FN)
        if np.isnan(PPV):
            print('NaN encountered for threshold T='+str(T)+', using previous T for setting PPV value')
            PPV = PRC[1,c-1]
        PRC[0,c] = TPR
        PRC[1,c] = PPV
            

    acu = np.abs(np.trapz(y=PRC[1,:], x=PRC[0,:], dx=0.001))
    return PRC, acu


def count_sources_in_labels(labels, fwd):
    """Counts the number of active sources in each label of labels.

    Parameters
    ----------
    labels : list
        List containing the labels.
    fwd : instance of Forward object

    Returns
    -------
    vert_nrs : array, shape (n_labels)
        Number of active sources in each label.
    """
    vert_nrs = []
    for label in labels: 
        if label.hemi == 'lh': 
            vert_nrs.append(np.sum(np.isin(label.vertices, fwd['src'][0]['vertno']))) 
        if label.hemi == 'rh': 
            vert_nrs.append(np.sum(np.isin(label.vertices, fwd['src'][1]['vertno']))) 
    return np.array(vert_nrs)

