import tempfile
import shutil
import subprocess
import sys
import hashlib
import os
import pickle
from io import StringIO
import collections

import pandas as pd
from pandas.errors import EmptyDataError
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from skbio.stats.distance import DistanceMatrix
from skbio.tree import TreeNode

from ggmap.snippets import (pandas2biom, cluster_run, biom2pandas)


plt.switch_backend('Agg')
plt.rc('font', family='DejaVu Sans')

FILE_REFERENCE_TREE = None
QIIME_ENV = 'qiime_env'


def _get_ref_phylogeny(file_tree=None):
    """Use QIIME config to infer location of reference tree or pass given tree.
    """
    global FILE_REFERENCE_TREE
    if file_tree is not None:
        return file_tree
    if FILE_REFERENCE_TREE is None:
        with subprocess.Popen(("source activate %s && "
                               "print_qiime_config.py "
                               "| grep 'pick_otus_reference_seqs_fp:'" %
                               QIIME_ENV),
                              shell=True,
                              stdout=subprocess.PIPE,
                              executable="bash") as call_x:
            out, err = call_x.communicate()
            if (call_x.wait() != 0):
                raise ValueError("_get_ref_phylogeny(): something went wrong")

            # convert from b'' to string
            out = out.decode()
            # split key:\tvalue
            out = out.split('\t')[1]
            # remove trailing \n
            out = out.rstrip()
            # chop '/rep_set/97_otus.fasta' from found path
            out = '/'.join(out.split('/')[:-2])
            FILE_REFERENCE_TREE = out + '/trees/97_otus.tree'
    return FILE_REFERENCE_TREE


def _parse_alpha(num_iterations, workdir, rarefaction_depth):
    coll = dict()
    if rarefaction_depth is None:
        try:
            x = pd.read_csv('%s/alpha_input.txt' % (
                workdir), sep='\t', index_col=0)
            return x
        except EmptyDataError as e:
            raise EmptyDataError(str(e) +
                                 "\nMaybe your reference tree is wrong?!")

    for iteration in range(num_iterations):
        try:
            x = pd.read_csv('%s/alpha_rarefaction_%i_%i.txt' % (
                workdir,
                rarefaction_depth,
                iteration), sep='\t', index_col=0)
        except EmptyDataError as e:
            raise EmptyDataError(str(e) +
                                 "\nMaybe your reference tree is wrong?!")
        if iteration == 0:
            for metric in x.columns:
                coll[metric] = pd.DataFrame(data=x[metric])
                coll[metric].columns = [iteration]
        else:
            for metric in x.columns:
                coll[metric][iteration] = x[metric]

    result = pd.DataFrame(index=list(coll.values())[0].index)
    for metric in coll.keys():
        result[metric] = coll[metric].mean(axis=1)

    return result


def _getremaining(counts_sums):
    """Compute number of samples that have at least X read counts.

    Parameters
    ----------
    counts_sum : Pandas.Series
        Reads per sample.

    Returns
    -------
    Pandas.Series:
        Index = sequencing depths,
        Values = number samples with at least this sequencing depth.
    """
    d = dict()
    remaining = counts_sums.shape[0]
    numdepths = counts_sums.value_counts().sort_index()
    for depth, numsamples in numdepths.iteritems():
        d[depth] = remaining
        remaining -= numsamples
    return pd.Series(data=d, name='remaining').to_frame()


def _parse_alpha_div_collated(filename, metric=None):
    """Parse QIIME's alpha_div_collated file for plotting with matplotlib.

    Parameters
    ----------
    filename : str
        Filename of the alpha_div_collated file to be parsed. It is the result
        of QIIME's collate_alpha.py script.
    metric : str
        Provide the alpha diversity metric name, used to create the input file.
        Default is None, i.e. the metric name is guessed from the filename.

    Returns
    -------
    Pandas.DataFrame with the averaged (over all iterations) alpha diversities
    per rarefaction depth per sample.

    Raises
    ------
    IOError
        If the file cannot be read.
    """
    try:
        # read qiime's alpha div collated file. It is tab separated and nan
        # values come as 'n/a'
        x = pd.read_csv(filename, sep='\t', na_values=['n/a'])

        # make a two level index
        x.set_index(keys=['sequences per sample', 'iteration'], inplace=True)

        # remove the column that reports the single rarefaction files,
        # because it would otherwise become another sample
        del x['Unnamed: 0']

        # average over all X iterations
        x = x.groupby(['sequences per sample']).mean()

        # change pandas format of data for easy plotting
        x = x.stack().to_frame().reset_index()

        # guess metric name from filename
        if metric is None:
            metric = filename.split('/')[-1].split('.')[0]

        # give columns more appropriate names
        x = x.rename(columns={'sequences per sample': 'rarefaction depth',
                              'level_1': 'sample_name',
                              0: metric})
        return x
    except IOError:
        raise IOError('Cannot read file "%s"' % filename)


def _plot_rarefaction_curves(data):
    """Plot rarefaction curves along with loosing sample stats + read count
       histogram.

    Parameters
    ----------
    data : dict()
        The result of rarefaction_curves(), i.e. a dict with the three keys
        - metrics
        - remaining
        - readcounts

    Returns
    -------
    Matplotlib figure
    """
    fig, axes = plt.subplots(2+len(data['metrics']),
                             1,
                             figsize=(5, (2+len(data['metrics']))*5),
                             sharex=False)

    # read count histogram
    ax = axes[0]
    n, bins, patches = ax.hist(data['readcounts'],
                               50,
                               facecolor='black',
                               alpha=0.75)
    ax.set_title('Read count distribution across samples')
    ax.set_xlabel("read counts")
    ax.set_ylabel("# samples")
    ax.get_xaxis().set_major_formatter(
        FuncFormatter(lambda x, p: format(int(x), ',')))

    # loosing samples
    ax = axes[1]
    x = data['remaining']
    x['lost'] = data['readcounts'].shape[0] - x['remaining']
    x.index.name = 'readcounts'
    ax.plot(x.index, x['remaining'], label='remaining')
    ax.plot(x.index, x['lost'], label='lost')
    ax.set_xlabel("rarefaction depth")
    ax.set_ylabel("# samples")
    ax.set_title('How many of the %i samples do we loose?' %
                 data['readcounts'].shape[0])
    ax.get_xaxis().set_major_formatter(
        FuncFormatter(lambda x, p: format(int(x), ',')))
    lostHalf = abs(x['remaining'] - x['lost'])
    lostHalf = lostHalf[lostHalf == lostHalf.min()].index[0]
    ax.set_xlim(0, lostHalf * 1.1)
    # p = ax.set_xscale("log", nonposx='clip')

    for i, metric in enumerate(data['metrics'].keys()):
        for sample, g in data['metrics'][metric].groupby('sample_name'):
            axes[i+2].errorbar(g['rarefaction depth'], g[g.columns[-1]])
        axes[i+2].set_ylabel(g.columns[-1])
        axes[i+2].set_xlabel('rarefaction depth')
        axes[i+2].set_xlim(0, lostHalf * 1.1)
        axes[i+2].get_xaxis().set_major_formatter(
            FuncFormatter(lambda x, p: format(int(x), ',')))

    return fig


def rarefaction_curves(counts,
                       metrics=["PD_whole_tree", "shannon", "observed_otus"],
                       num_steps=20, num_threads=10,
                       dry=True, use_grid=True, nocache=False,
                       reference_tree=None, max_depth=None, workdir=None,
                       wait=True, crawl_dir=None):
    """Produce rarefaction curves, i.e. reads/sample and alpha vs. depth plots.

    Parameters
    ----------
    counts : Pandas.DataFrame
        The raw read counts. Columns are samples, rows are features.
    metrics : [str]
        List of alpha diversity metrics to use.
        Default is ["PD_whole_tree", "shannon", "observed_otus"]
    num_steps : int
        Number of different rarefaction steps to test. The higher the slower.
        Default is 20.
    num_threads : int
        Number of cores to use for parallelization. Default is 10.
    dry : bool
        If True: only prepare working directory and create necessary input
        files and print the command that would be executed in a non dry run.
        For debugging. Workdir is not deleted.
        "pre_execute" is called, but not "post_execute".
        Default is True.
    use_grid : bool
        If True, use qsub to schedule as a grid job, otherwise run locally.
        Default is True.
    nocache : bool
        Normally, successful results are cached in .anacache directory to be
        retrieved when called a second time. You can deactivate this feature
        (useful for testing) by setting "nocache" to False.
    reference_tree : str
        Filepath to a newick tree file, which will be the phylogeny for unifrac
        alpha diversity distances. By default, qiime's GreenGenes tree is used.
    max_depth : int
        Maximal rarefaction depth. By default counts.sum().describe()['75%'] is
        used.
    workdir : str
        Filepath to an existing temporary working directory. This will only
        call post_execute to parse results.
    crawl_dir : dir
        A directory with workdir's in from which results should be parsed if
        a) execution complete (i.e. finished.info file exists)
        b) cache signature matches
        Default is None

    Returns
    -------
    plt figure
    """
    def pre_execute(workdir, args):
        # store counts as a biom file
        pandas2biom(workdir+'/input.biom', args['counts'])

    def commands(workdir, ppn, args):
        max_rare_depth = args['counts'].sum().describe()['75%']
        if args['max_depth'] is not None:
            max_rare_depth = args['max_depth']
        commands = []

        # Alpha rarefaction command
        commands.append(('parallel_multiple_rarefactions.py '
                         '-T '
                         '-i %s '      # Input filepath, (the otu table)
                         '-m %i '      # Min seqs/sample
                         '-x %i '      # Max seqs/sample (inclusive)
                         '-s %i '      # Levels: min, min+step... for level
                                       # <= max
                         '-o %s '      # Write output rarefied otu tables here
                                       # makes dir if it doesn’t exist
                         '--jobs_to_start %i') % (  # Number of jobs to start
            workdir+'/input.biom',
            max(1000, args['counts'].sum().min()),
            max_rare_depth,
            (max_rare_depth - args['counts'].sum().min())/args['num_steps'],
            workdir+'/rare/rarefaction/',
            ppn))

        # Alpha diversity on rarefied OTU tables command
        commands.append(('parallel_alpha_diversity.py '
                         '-T '
                         '-i %s '         # Input path, must be directory
                         '-o %s '         # Output path, must be directory
                         '--metrics %s '  # Metrics to use, comma delimited
                         '-t %s '         # Path to newick tree file, required
                                          # for phylogenetic metrics
                         '--jobs_to_start %i') % (  # Number of jobs to start
            workdir+'/rare/rarefaction/',
            workdir+'/rare/alpha_div/',
            ",".join(args['metrics']),
            _get_ref_phylogeny(reference_tree),
            ppn))

        # Collate alpha command
        commands.append(('collate_alpha.py '
                         '-i %s '      # Input path (a directory)
                         '-o %s') % (  # Output path (a directory).
                                       # will be created if needed
            workdir+'/rare/alpha_div/',
            workdir+'/rare/alpha_div_collated/'))

        return commands

    def post_execute(workdir, args, pre_data):
        sums = args['counts'].sum()
        results = {'metrics': dict(),
                   'remaining': _getremaining(sums),
                   'readcounts': sums}
        for metric in args['metrics']:
            results['metrics'][metric] = _parse_alpha_div_collated(
                workdir+'/rare/alpha_div_collated/'+metric+'.txt')

        return results

    data = _executor('rare',
                     {'counts': counts,
                      'metrics': metrics,
                      'num_steps': num_steps,
                      'max_depth': max_depth},
                     pre_execute,
                     commands,
                     post_execute,
                     dry=dry,
                     use_grid=use_grid,
                     ppn=num_threads,
                     nocache=nocache,
                     workdir=workdir,
                     wait=wait,
                     crawl_dir=crawl_dir)

    if dry is not True:
        return _plot_rarefaction_curves(data)


def rarefy(counts, rarefaction_depth,
           dry=True, use_grid=True, nocache=False, workdir=None, wait=True,
           crawl_dir=None):
    """Rarefies a given OTU table to a given depth. This depth should be
       determined by looking at rarefaction curves.

    Paramaters
    ----------
    counts : Pandas.DataFrame
        OTU counts
    rarefaction_depth : int
        Rarefaction depth that must be applied to counts.
    dry : boolean
        Do NOT run clusterjobs, just print commands. Default: True
    use_grid : boolean
        Use grid engine instead of local execution. Default: True
    nocache : boolean
        Don't use cache to retrieve results for previously computed inputs.
    workdir : str
        Path to the working dir of an unfinished previous computation.
    crawl_dir : dir
        A directory with workdir's in from which results should be parsed if
        a) execution complete (i.e. finished.info file exists)
        b) cache signature matches
        Default is None

    Returns
    -------
    Pandas.DataFrame: Rarefied OTU table."""

    def pre_execute(workdir, args):
        # store counts as a biom file
        pandas2biom(workdir+'/input.biom', args['counts'])

    def commands(workdir, ppn, args):
        commands = []
        commands.append(('multiple_rarefactions.py '
                         '-i %s '                    # input biom file
                         '-m %i '                    # min rarefaction depth
                         '-x %i '                    # max rarefaction depth
                         '-s 1 '                     # depth steps
                         '-o %s '                    # output directory
                         '-n 1 '                  # number iterations per depth
                         ) % (   # number parallel jobs
            workdir+'/input.biom',
            args['rarefaction_depth'],
            args['rarefaction_depth'],
            workdir+'/rarefactions'))

        return commands

    def post_execute(workdir, args, pre_data):
        return biom2pandas(workdir+'/rarefactions/rarefaction_%i_0.biom' %
                           args['rarefaction_depth'])

    return _executor('rarefy',
                     {'counts': counts,
                      'rarefaction_depth': rarefaction_depth},
                     pre_execute,
                     commands,
                     post_execute,
                     dry=dry,
                     use_grid=use_grid,
                     ppn=1,
                     nocache=nocache,
                     workdir=workdir,
                     wait=wait,
                     crawl_dir=crawl_dir)


def alpha_diversity(counts, rarefaction_depth,
                    metrics=["PD_whole_tree", "shannon", "observed_otus"],
                    num_threads=10, num_iterations=10, dry=True,
                    use_grid=True, nocache=False, workdir=None,
                    reference_tree=None, wait=True, crawl_dir=None):
    """Computes alpha diversity values for given BIOM table.

    Paramaters
    ----------
    counts : Pandas.DataFrame
        OTU counts
    rarefaction_depth : int
        Rarefaction depth that must be applied to counts.
    metrics : [str]
        Alpha diversity metrics to be computed.
    num_threads : int
        Number of parallel threads. Default: 10.
    num_iterations : int
        Number of iterations to rarefy the input table.
    dry : boolean
        Do NOT run clusterjobs, just print commands. Default: True
    use_grid : boolean
        Use grid engine instead of local execution. Default: True
    reference_tree : str
        Reference tree file name for phylogenetic metics like unifrac.
    crawl_dir : dir
        A directory with workdir's in from which results should be parsed if
        a) execution complete (i.e. finished.info file exists)
        b) cache signature matches
        Default is None

    Returns
    -------
    Pandas.DataFrame: alpha diversity values for each sample (rows) for every
    chosen metric (columns)."""

    def pre_execute(workdir, args):
        # store counts as a biom file
        pandas2biom(workdir+'/input.biom', args['counts'])

        # create a mock metadata file
        metadata = pd.DataFrame(index=args['counts'].columns)
        metadata.index.name = '#SampleID'
        metadata['mock'] = 'foo'
        metadata.to_csv(workdir+'/metadata.tsv', sep='\t')

    def commands(workdir, ppn, args):
        commands = []
        if args['rarefaction_depth'] is not None:
            commands.append(('parallel_multiple_rarefactions.py '
                             '-T '      # direct polling
                             '-i %s '   # input biom file
                             '-m %i '   # min rarefaction depth
                             '-x %i '   # max rarefaction depth
                             '-s 1 '    # depth steps
                             '-o %s '   # output directory
                             '-n %i '   # number iterations per depth
                             '--jobs_to_start %i') % (   # number parallel jobs
                workdir+'/input.biom',
                args['rarefaction_depth'],
                args['rarefaction_depth'],
                workdir+'/rarefactions',
                args['num_iterations'],
                ppn))

        dir_bioms = workdir+'/rarefactions'
        if args['rarefaction_depth'] is None:
            dir_bioms = workdir
        commands.append(('parallel_alpha_diversity.py '
                         '-T '                      # direct polling
                         '-i %s '                   # dir to rarefied tables
                         '-o %s '                   # output directory
                         '--metrics %s '            # list of alpha div metrics
                         '-t %s '                   # tree reference file
                         '--jobs_to_start %i') % (  # number parallel jobs
            dir_bioms,
            workdir+'/alpha_div/',
            ",".join(args['metrics']),
            _get_ref_phylogeny(args['reference_tree']),
            ppn))
        return commands

    def post_execute(workdir, args, pre_data):
        res = _parse_alpha(args['num_iterations'],
                           workdir+'/alpha_div/',
                           args['rarefaction_depth'])
        if res is not None:
            res.index.name = args['counts'].index.name
        return res

    return _executor('adiv',
                     {'counts': counts,
                      'metrics': metrics,
                      'rarefaction_depth': rarefaction_depth,
                      'num_iterations': num_iterations,
                      'reference_tree': reference_tree},
                     pre_execute,
                     commands,
                     post_execute,
                     dry=dry,
                     use_grid=use_grid,
                     ppn=num_threads,
                     nocache=nocache,
                     workdir=workdir,
                     wait=wait,
                     crawl_dir=crawl_dir)


def beta_diversity(counts,
                   metrics=["unweighted_unifrac",
                            "weighted_unifrac",
                            "bray_curtis"],
                   num_threads=10, dry=True, use_grid=True, nocache=False,
                   reference_tree=None, workdir=None,
                   wait=True, use_parallel=False, crawl_dir=None):
    """Computes beta diversity values for given BIOM table.

    Parameters
    ----------
    counts : Pandas.DataFrame
        OTU counts
    metrics : [str]
        Beta diversity metrics to be computed.
    num_threads : int
        Number of parallel threads. Default: 10.
    dry : boolean
        Do NOT run clusterjobs, just print commands. Default: True
    use_grid : boolean
        Use grid engine instead of local execution. Default: True
    reference_tree : str
        Reference tree file name for phylogenetic metics like unifrac.
    use_parallel : boolean
        Default: false. If true, use parallel version of beta div computation.
        I found that it often stalles with defunct processes.
    crawl_dir : dir
        A directory with workdir's in from which results should be parsed if
        a) execution complete (i.e. finished.info file exists)
        b) cache signature matches
        Default is None

    Returns
    -------
    Dict of Pandas.DataFrame, one per metric."""

    def pre_execute(workdir, args):
        # store counts as a biom file
        pandas2biom(workdir+'/input.biom', args['counts'])

    if use_parallel:
        def commands(workdir, ppn, args):
            commands = []
            commands.append(('parallel_beta_diversity.py '
                             '-i %s '              # input biom file
                             '-m %s '              # list of beta div metrics
                             '-t %s '              # tree reference file
                             '-o %s '
                             '-O %i ') % (
                workdir+'/input.biom',
                ",".join(args['metrics']),
                _get_ref_phylogeny(reference_tree),
                workdir+'/beta',
                ppn))
            return commands
    else:
        def commands(workdir, ppn, args):
            commands = []
            commands.append(('beta_diversity.py '
                             '-i %s '              # input biom file
                             '-m %s '              # list of beta div metrics
                             '-t %s '              # tree reference file
                             '-o %s ') % (
                workdir+'/input.biom',
                ",".join(args['metrics']),
                _get_ref_phylogeny(reference_tree),
                workdir+'/beta'))
            return commands

    def post_execute(workdir, args, pre_data):
        results = dict()
        for metric in args['metrics']:
            results[metric] = DistanceMatrix.read('%s/%s_input.txt' % (
                workdir+'/beta',
                metric))
        return results

    return _executor('bdiv',
                     {'counts': counts,
                      'metrics': metrics},
                     pre_execute,
                     commands,
                     post_execute,
                     dry=dry,
                     use_grid=use_grid,
                     ppn=num_threads,
                     nocache=nocache,
                     workdir=workdir,
                     wait=wait,
                     crawl_dir=crawl_dir)


def sepp(counts,
         dry=True, use_grid=True, nocache=False, workdir=None,
         ppn=10, pmem='20GB', wait=True, walltime='12:00:00', crawl_dir=None):
    """Tip insertion of deblur sequences into GreenGenes backbone tree.

    Parameters
    ----------
    counts : Pandas.DataFrame | Pandas.Series
        a) OTU counts in form of a Pandas.DataFrame.
        b) If providing a Pandas.Series, we expect the index to be a fasta
           headers and the colum the fasta sequences.
    dry : bool
        If True: only prepare working directory and create necessary input
        files and print the command that would be executed in a non dry run.
        For debugging. Workdir is not deleted.
        "pre_execute" is called, but not "post_execute".
        Default is True.
    use_grid : bool
        If True, use qsub to schedule as a grid job, otherwise run locally.
        Default is True.
    nocache : bool
        Normally, successful results are cached in .anacache directory to be
        retrieved when called a second time. You can deactivate this feature
        (useful for testing) by setting "nocache" to False.
    workdir : str
        Filepath to an existing temporary working directory. This will only
        call post_execute to parse results.
    wait : bool
        Default: True. Wait for results.
    walltime : str
        hh:mm:ss formated wall runtime on cluster. Default is 12:00:00.
    crawl_dir : dir
        A directory with workdir's in from which results should be parsed if
        a) execution complete (i.e. finished.info file exists)
        b) cache signature matches
        Default is None

    Returns
    -------
    ???"""
    def pre_execute(workdir, args):
        # write all deblur sequences into one file
        file_fragments = workdir + '/sequences.mfa'
        f = open(file_fragments, 'w')
        if type(args['seqs']) == pd.Series:
            for header, sequence in args['seqs'].iteritems():
                f.write('>%s\n%s\n' % (header, sequence))
        else:
            for sequence in args['seqs']:
                f.write('>%s\n%s\n' % (sequence, sequence))
        f.close()

    def commands(workdir, ppn, args):
        commands = []
        commands.append('cd %s' % workdir)
        commands.append('%srun-sepp.sh "%s" res -x %i' % (
            '/home/sjanssen/miniconda3/envs/seppGG_py3/src/sepp-package/',
            workdir+'/sequences.mfa',
            ppn))
        return commands

    def post_execute(workdir, args, pre_data):
        # read the resuling insertion tree as an skbio TreeNode object
        tree = TreeNode.read(workdir+'/res_placement.tog.relabelled.tre')

        # use the phylogeny to determine tips lineage
        lineages = []
        features = []
        for i, tip in enumerate(tree.tips()):
            if tip.name.isdigit():
                continue

            lineage = []
            for ancestor in tip.ancestors():
                try:
                    float(ancestor.name)
                except TypeError:
                    pass
                except ValueError:
                    lineage.append(ancestor.name)

            lineages.append("; ".join(reversed(lineage)))
            features.append(tip.name)

        # storing tree as newick string is necessary since large trees would
        # result in too many recursions for the python heap :-/
        newick = StringIO()
        tree.write(newick)
        return {'taxonomy': pd.DataFrame(data=lineages,
                                         index=features,
                                         columns=['taxonomy']),
                'tree': newick.getvalue()}

    inp = sorted(counts.index)
    if type(counts) == pd.Series:
        # typically, the input is an OTU table with index holding sequences.
        # However, if provided a Pandas.Series, we expect index are sequence
        # headers and single column holds sequences.
        inp = counts.sort_index()
    res = _executor('sepp',
                    {'seqs': inp},
                    pre_execute,
                    commands,
                    post_execute,
                    dry=dry,
                    use_grid=use_grid,
                    ppn=ppn,
                    pmem=pmem,
                    nocache=nocache,
                    walltime=walltime,
                    environment='seppGG_py3',
                    workdir=workdir,
                    wait=wait,
                    crawl_dir=crawl_dir)
    if (wait is False) or dry:
        return res

    res['tree'] = TreeNode.read(StringIO(res['tree']))
    return res


def _executor(jobname, cache_arguments, pre_execute, commands, post_execute,
              dry=True, use_grid=True, ppn=10, nocache=False, workdir=None,
              pmem='8GB', environment=QIIME_ENV, walltime='4:00:00',
              wait=True, crawl_dir=None):
    """

    Parameters
    ----------
    workdir : str
        Dir path name. Default is None, i.e. new results need to be computed.
        If a dir is given, we assume the same call has been already run - at
        least partially and we want to resume from this point, i.e. collect
        results.
    """
    DIR_CACHE = '.anacache'
    FILE_STATUS = 'finished.info'

    # caching
    _input = collections.OrderedDict(sorted(cache_arguments.items()))
    file_cache = "%s/%s.%s" % (DIR_CACHE, hashlib.md5(
        str(_input).encode()).hexdigest(), jobname)

    if os.path.exists(file_cache) and (nocache is not True):
        sys.stderr.write("Using existing results from '%s'. \n" % file_cache)
        f = open(file_cache, 'rb')
        results = pickle.load(f)
        f.close()
        return results

    # create a temporary working directory
    pre_data = None
    if (workdir is None) and (crawl_dir is None):
        prefix = 'ana_%s_' % jobname
        if use_grid:
            workdir = tempfile.mkdtemp(prefix=prefix,
                                       dir='/home/sjanssen/TMP/')
        else:
            workdir = tempfile.mkdtemp(prefix=prefix)
        sys.stderr.write("Working directory is '%s'. " % workdir)
        # leave an empty file in workdir with cache file name to later
        # parse results from tmp dir via 'crawl_dir'
        f = open(workdir + '/' + file_cache.split('/')[-1], 'w')
        f.close()

        pre_data = pre_execute(workdir, cache_arguments)

        lst_commands = commands(workdir, ppn, cache_arguments)
        # device creation of a file _after_ execution of the job in workdir
        lst_commands.append('touch %s/%s' % (workdir, FILE_STATUS))
        if not use_grid:
            if dry:
                sys.stderr.write("\n\n".join(lst_commands))
                return None
            with subprocess.Popen("source activate %s && %s" %
                                  (environment, " && ".join(lst_commands)),
                                  shell=True,
                                  stdout=subprocess.PIPE,
                                  executable="bash") as call_x:
                if (call_x.wait() != 0):
                    raise ValueError("something went wrong")
        else:
            qid = cluster_run(lst_commands, 'ana_%s' % jobname, workdir+'mock',
                              environment, ppn=ppn, wait=wait, dry=dry,
                              pmem=pmem, walltime=walltime,
                              file_qid=workdir+'/cluster_job_id.txt')
            if dry:
                return None
            if wait is False:
                return qid
    elif crawl_dir is not None:
        if not os.path.exists(crawl_dir):
            raise IOError("crawl_dir '%s' does not exist!" % crawl_dir)
        else:
            # crawl through given directory and search for those sub-dirs that
            # contain .anacache subdir and only one file, identical with the
            # file_cache for the current input
            pot_workdirs = [x[0]  # report directory name
                            for x in os.walk(crawl_dir)
                            # shares same cache signature:
                            if file_cache.split('/')[-1] in x[2]
                            # is completed
                            if 'finished.info' in x[2]]
            if len(pot_workdirs) > 0:
                workdir = pot_workdirs[0]
                sys.stderr.write('found matching working dir "%s"\n' % workdir)
                pre_data = pre_execute(workdir, cache_arguments)
            else:
                raise ValueError("No suitable working directory found!")
    else:
        pre_data = pre_execute(workdir, cache_arguments)

    results = post_execute(workdir, cache_arguments, pre_data)

    if results is not None:
        shutil.rmtree(workdir)
        sys.stderr.write(" Was removed.\n")

    os.makedirs(os.path.dirname(file_cache), exist_ok=True)
    f = open(file_cache, 'wb')
    pickle.dump(results, f)
    f.close()

    return results
