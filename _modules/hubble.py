# -*- encoding: utf-8 -*-
'''
Loader and primary interface for nova modules

:maintainer: HubbleStack / basepi
:maturity: 2016.10.2
:platform: All
:requires: SaltStack

See README for documentation

Configuration:
    - hubblestack:nova:module_dir
    - hubblestack:nova:profile_dir
    - hubblestack:nova:saltenv
    - hubblestack:nova:autoload
    - hubblestack:nova:autosync
'''
from __future__ import absolute_import
import logging

log = logging.getLogger(__name__)

import imp
import os
import sys
import six
import inspect
import yaml
import traceback

import salt
import salt.utils
from salt.exceptions import CommandExecutionError
from nova_loader import NovaLazyLoader

__nova__ = {}
__version__ = 'v2017.9.0'


def audit(configs=None,
          tags='*',
          verbose=None,
          show_success=None,
          show_compliance=None,
          show_profile=None,
          called_from_top=None,
          debug=None,
          **kwargs):
    '''
    Primary entry point for audit calls.

    configs
        List (comma-separated or python list) of yaml configs/directories to
        search for audit data. Directories are dot-separated, much in the same
        way as Salt states. For individual config names, leave the .yaml
        extension off.  If a given path resolves to a python file, it will be
        treated as a single config. Otherwise it will be treated as a
        directory. All configs found in a recursive search of the specified
        directories will be included in the audit.

        If configs is not provided, this function will call ``hubble.top``
        instead.

    tags
        Glob pattern string for tags to include in the audit. This way you can
        give a directory, and tell the system to only run the `CIS*`-tagged
        audits, for example.

    verbose
        Whether to show additional information about audits, including
        description, remediation instructions, etc. The data returned depends
        on the audit module. Defaults to False. Configurable via
        `hubblestack:nova:verbose` in minion config/pillar.

    show_success
        Whether to show successful audits in addition to failed audits.
        Defaults to True. Configurable via `hubblestack:nova:show_success` in
        minion config/pillar.

    show_compliance
        Whether to show compliance as a percentage (successful checks divided
        by total checks). Defaults to True. Configurable via
        `hubblestack:nova:show_compliance` in minion config/pillar.

    show_profile
        DEPRECATED

    called_from_top
        Ignore this argument. It is used for distinguishing between user-calls
        of this function and calls from hubble.top.

    debug
        Whether to log additional information to help debug nova. Defaults to
        False. Configurable via `hubblestack:nova:debug` in minion
        config/pillar.

    **kwargs
        Any parameters & values that are not explicitly defined will be passed
        directly through to the Nova module(s).

    CLI Examples:

    .. code-block:: bash

        salt '*' hubble.audit foo
        salt '*' hubble.audit foo,bar tags='CIS*'
        salt '*' hubble.audit foo,bar.baz verbose=True
    '''
    if configs is None:
        return top(verbose=verbose,
                   show_success=show_success,
                   show_compliance=show_compliance)

    if not called_from_top and __salt__['config.get']('hubblestack:nova:autoload', True):
        load()
    if not __nova__:
        return False, 'No nova modules/data have been loaded.'

    if verbose is None:
        verbose = __salt__['config.get']('hubblestack:nova:verbose', False)
    if show_success is None:
        show_success = __salt__['config.get']('hubblestack:nova:show_success', True)
    if show_compliance is None:
        show_compliance = __salt__['config.get']('hubblestack:nova:show_compliance', True)
    if show_profile is not None:
        log.warning(
            'Keyword argument \'show_profile\' is no longer supported'
        )
    if debug is None:
        debug = __salt__['config.get']('hubblestack:nova:debug', False)

    if not isinstance(configs, list):
        # Convert string
        configs = configs.split(',')

    # Convert config list to paths, with leading slashes
    configs = [os.path.join(os.path.sep, os.path.join(*(con.split('.yaml')[0]).split('.')))
               for con in configs]

    # Pass any module parameters through to the Nova module
    nova_kwargs = {}
    # Get values from config first (if any) and merge into nova_kwargs
    nova_kwargs_config =  __salt__['config.get']('hubblestack:nova:nova_kwargs', False)
    if nova_kwargs_config is not False:
        nova_kwargs.update(nova_kwargs_config)
    # Now process arguments from CLI and merge into nova_kwargs_dict
    if kwargs is not None:
        nova_kwargs.update(kwargs)

    log.debug('nova_kwargs: ' + str(nova_kwargs))

    ret = _run_audit(configs, tags, debug, **nova_kwargs)

    terse_results = {}
    verbose_results = {}

    # Pull out just the tag and description
    terse_results['Failure'] = []
    tags_descriptions = set()

    for tag_data in ret.get('Failure', []):
        tag = tag_data['tag']
        description = tag_data.get('description')
        if (tag, description) not in tags_descriptions:
            terse_results['Failure'].append({tag: description})
            tags_descriptions.add((tag, description))

    terse_results['Success'] = []
    tags_descriptions = set()

    for tag_data in ret.get('Success', []):
        tag = tag_data['tag']
        description = tag_data.get('description')
        if (tag, description) not in tags_descriptions:
            terse_results['Success'].append({tag: description})
            tags_descriptions.add((tag, description))

    terse_results['Controlled'] = []
    control_reasons = set()

    for tag_data in ret.get('Controlled', []):
        tag = tag_data['tag']
        control_reason = tag_data.get('control', '')
        description = tag_data.get('description')
        if (tag, description, control_reason) not in control_reasons:
            terse_results['Controlled'].append({tag: control_reason})
            control_reasons.add((tag, description, control_reason))

    # Calculate compliance level
    if show_compliance:
        compliance = _calculate_compliance(terse_results)
    else:
        compliance = False

    if not show_success and 'Success' in terse_results:
        terse_results.pop('Success')

    if not terse_results['Controlled']:
        terse_results.pop('Controlled')

    # Format verbose output as single-key dictionaries with tag as key
    if verbose:
        verbose_results['Failure'] = []

        for tag_data in ret.get('Failure', []):
            tag = tag_data['tag']
            verbose_results['Failure'].append({tag: tag_data})

        verbose_results['Success'] = []

        for tag_data in ret.get('Success', []):
            tag = tag_data['tag']
            verbose_results['Success'].append({tag: tag_data})

        if not show_success and 'Success' in verbose_results:
            verbose_results.pop('Success')

        verbose_results['Controlled'] = []

        for tag_data in ret.get('Controlled', []):
            tag = tag_data['tag']
            verbose_results['Controlled'].append({tag: tag_data})

        if not verbose_results['Controlled']:
            verbose_results.pop('Controlled')

        results = verbose_results
    else:
        results = terse_results

    if compliance:
        results['Compliance'] = compliance

    if not called_from_top and not results:
        results['Messages'] = 'No audits matched this host in the specified profiles.'

    for error in ret.get('Errors', []):
      if not results.has_key('Errors'):
        results['Errors'] = []
      results['Errors'].append(error)

    return results

def _run_audit(configs, tags, debug, **kwargs):

    results = {}

    # Compile a list of audit data sets which we need to run
    to_run = set()
    for config in configs:
        found_for_config = False
        for key in __nova__.__data__:
            key_path_split = key.split('.yaml')[0].split(os.path.sep)
            matches = True
            if config != os.path.sep:
                for i, path in enumerate(config.split(os.path.sep)):
                    if i >= len(key_path_split) or path != key_path_split[i]:
                        matches = False
            if matches:
                # Found a match, add the audit data to the set
                found_for_config = True
                to_run.add(key)
        if not found_for_config:
            # No matches were found for this entry, add an error
            if 'Errors' not in results:
                results['Errors'] = []
            results['Errors'].append({config: {'error': 'No matching profiles found for {0}'
                                                        .format(config)}})

    # compile list of tuples with profile name and profile data
    data_list = [(key.split('.yaml')[0].split(os.path.sep)[-1],
                  __nova__.__data__[key]) for key in to_run]
    if debug:
        log.debug('hubble.py configs:')
        log.debug(configs)
        log.debug('hubble.py data_list:')
        log.debug(data_list)
    # Run the audits
    # This is currently pretty brute-force -- we just run all the modules we
    # have available with the data list, so data will be processed multiple
    # times. However, for the scale we're working at this should be fine.
    # We can revisit if this ever becomes a big bottleneck
    for key, func in __nova__._dict.iteritems():
        try:
            ret = func(data_list, tags, **kwargs)
        except Exception as exc:
            log.error('Exception occurred in nova module:')
            log.error(traceback.format_exc())
            if 'Errors' not in results:
                results['Errors'] = []
            results['Errors'].append({key: {'error': 'exception occurred',
                                            'data': traceback.format_exc().splitlines()[-1]}})
            continue
        else:
            if not isinstance(ret, dict):
                if 'Errors' not in results:
                    results['Errors'] = []
                results['Errors'].append({key: {'error': 'bad return type',
                                                'data': ret}})
                continue

        # Merge in the results
        for key, val in ret.iteritems():
            if key not in results:
                results[key] = []
            results[key].extend(val)

    processed_controls = {}
    # Inspect the data for compensating control data
    for _, audit_data in data_list:
        control_config = audit_data.get('control', [])
        for control in control_config:
            if isinstance(control, str):
                processed_controls[control] = {}
            else:  # dict
                for control_tag, control_data in control.iteritems():
                    if isinstance(control_data, str):
                        processed_controls[control_tag] = {'reason': control_data}
                    else:  # dict
                        processed_controls[control_tag] = control_data

    if debug:
        log.debug('hubble.py control data:')
        log.debug(processed_controls)

    # Look through the failed results to find audits which match our control config
    failures_to_remove = []
    for i, failure in enumerate(results.get('Failure', [])):
        failure_tag = failure['tag']
        if failure_tag in processed_controls:
            failures_to_remove.append(i)
            if 'Controlled' not in results:
                results['Controlled'] = []
            failure.update({
                'control': processed_controls[failure_tag].get('reason')
            })
            results['Controlled'].append(failure)

    # Remove controlled failures from results['Failure']
    if failures_to_remove:
        for failure_index in reversed(sorted(set(failures_to_remove))):
            results['Failure'].pop(failure_index)

    for key in results.keys():
        if not results[key]:
            results.pop(key)

    return results


def top(topfile='top.nova',
        verbose=None,
        show_success=None,
        show_compliance=None,
        show_profile=None,
        debug=None):
    '''
    Compile and run all yaml data from the specified nova topfile.

    Nova topfiles look very similar to saltstack topfiles, except the top-level
    key is always ``nova``, as nova doesn't have a concept of environments.

    .. code-block:: yaml

        nova:
          '*':
            - cve_scan
            - cis_gen
          'web*':
            - firewall
            - cis-centos-7-l2-scored
            - cis-centos-7-apache24-l1-scored
          'G@os_family:debian':
            - netstat
            - cis-debian-7-l2-scored: 'CIS*'
            - cis-debian-7-mysql57-l1-scored: 'CIS 2.1.2'

    Additionally, all nova topfile matches are compound matches, so you never
    need to define a match type like you do in saltstack topfiles.

    Each list item is a string representing the dot-separated location of a
    yaml file which will be run with hubble.audit. You can also specify a
    tag glob to use as a filter for just that yaml file, using a colon
    after the yaml file (turning it into a dictionary). See the last two lines
    in the yaml above for examples.


    Arguments:

    topfile
        The path of the topfile, relative to your hubblestack_nova_profiles
        directory.

    verbose
        Whether to show additional information about audits, including
        description, remediation instructions, etc. The data returned depends
        on the audit module. Defaults to False. Configurable via
        `hubblestack:nova:verbose` in minion config/pillar.

    show_success
        Whether to show successful audits in addition to failed audits.
        Defaults to True. Configurable via `hubblestack:nova:show_success` in
        minion config/pillar.

    show_compliance
        Whether to show compliance as a percentage (successful checks divided
        by total checks). Defaults to True. Configurable via
        `hubblestack:nova:show_compliance` in minion config/pillar.

    show_profile
        DEPRECATED

    debug
        Whether to log additional information to help debug nova. Defaults to
        False. Configurable via `hubblestack:nova:debug` in minion
        config/pillar.

    CLI Examples:

    .. code-block:: bash

        salt '*' hubble.top
        salt '*' hubble.top foo/bar/top.nova
        salt '*' hubble.top foo/bar.nova verbose=True
    '''
    if __salt__['config.get']('hubblestack:nova:autoload', True):
        load()
    if not __nova__:
        return False, 'No nova modules/data have been loaded.'

    if verbose is None:
        verbose = __salt__['config.get']('hubblestack:nova:verbose', False)
    if show_success is None:
        show_success = __salt__['config.get']('hubblestack:nova:show_success', True)
    if show_compliance is None:
        show_compliance = __salt__['config.get']('hubblestack:nova:show_compliance', True)
    if show_profile is not None:
        log.warning(
            'Keyword argument \'show_profile\' is no longer supported'
        )
    if debug is None:
        debug = __salt__['config.get']('hubblestack:nova:debug', False)

    results = {}

    # Get a list of yaml to run
    top_data = _get_top_data(topfile)

    # Will be a combination of strings and single-item dicts. The strings
    # have no tag filters, so we'll treat them as tag filter '*'. If we sort
    # all the data by tag filter we can batch where possible under the same
    # tag.
    data_by_tag = {}
    for data in top_data:
        if isinstance(data, str):
            if '*' not in data_by_tag:
                data_by_tag['*'] = []
            data_by_tag['*'].append(data)
        elif isinstance(data, dict):
            for key, tag in data.iteritems():
                if tag not in data_by_tag:
                    data_by_tag[tag] = []
                data_by_tag[tag].append(key)
        else:
            if 'Errors' not in results:
                results['Errors'] = {}
            error_log = 'topfile malformed, list entries must be strings or '\
                        'dicts: {0}'.format(data)
            results['Errors'][topfile] = {'error': error_log}
            log.error(error_log)
            continue

    if not data_by_tag:
        return results

    # Run the audits
    for tag, data in data_by_tag.iteritems():
        ret = audit(configs=data,
                    tags=tag,
                    verbose=verbose,
                    show_success=True,
                    show_compliance=False,
                    called_from_top=True)

        # Merge in the results
        for key, val in ret.iteritems():
            if key not in results:
                results[key] = []
            results[key].extend(val)

    if show_compliance:
        compliance = _calculate_compliance(results)
        if compliance:
            results['Compliance'] = compliance

    for key in results.keys():
        if not results[key]:
            results.pop(key)

    if not results:
        results['Messages'] = 'No audits matched this host in the specified profiles.'

    if not show_success and 'Success' in results:
        results.pop('Success')

    return results


def sync(clean=False):
    '''
    Sync the nova audit modules and profiles from the saltstack fileserver.

    The modules should be stored in the salt fileserver. By default nova will
    search the base environment for a top level ``hubblestack_nova``
    directory, unless otherwise specified via pillar or minion config
    (``hubblestack:nova:module_dir``)

    The profiles should be stored in the salt fileserver. By default nova will
    search the base environment for a top level ``hubblestack_nova_profiles``
    directory, unless otherwise specified via pillar or minion config
    (``hubblestack:nova:profile_dir``)

    Modules and profiles will be cached in the normal minion cachedir

    Returns a boolean representing success

    NOTE: This function will optionally clean out existing files at the cached
    location, as cp.cache_dir doesn't clean out old files. Pass ``clean=True``
    to enable this behavior

    CLI Examples:

    .. code-block:: bash

        salt '*' nova.sync
        salt '*' nova.sync saltenv=hubble
    '''
    log.debug('syncing nova modules')
    nova_profile_dir = __salt__['config.get']('hubblestack:nova:profile_dir',
                                              'salt://hubblestack_nova_profiles')
    nova_module_dir = __salt__['config.get']('hubblestack:nova:module_dir',
                                             'salt://hubblestack_nova')
    saltenv = __salt__['config.get']('hubblestack:nova:saltenv', 'base')

    # Clean previously synced files
    if clean:
        for nova_dir in _hubble_dir():
            __salt__['file.remove'](nova_dir)

    synced = []
    for i, nova_dir in enumerate((nova_module_dir, nova_profile_dir)):
        # Support optional salt:// in config
        if 'salt://' in nova_dir:
            path = nova_dir
            _, _, nova_dir = nova_dir.partition('salt://')
        else:
            path = 'salt://{0}'.format(nova_dir)

        # Sync the files
        cached = __salt__['cp.cache_dir'](path, saltenv=saltenv)

        if cached and isinstance(cached, list):
            # Success! Trim the paths
            cachedir = os.path.dirname(_hubble_dir()[i])
            ret = [relative.partition(cachedir)[2] for relative in cached]
            synced.extend(ret)
        else:
            if isinstance(cached, list):
                # Nothing was found
                synced.extend(cached)
            else:
                # Something went wrong, there's likely a stacktrace in the output
                # of cache_dir
                raise CommandExecutionError('An error occurred while syncing: {0}'
                                            .format(cached))
    return synced


def load():
    '''
    Load the synced audit modules.
    '''
    if __salt__['config.get']('hubblestack:nova:autosync', True):
        sync()

    for nova_dir in _hubble_dir():
        if not os.path.isdir(nova_dir):
            return False, 'No synced nova modules/profiles found'

    log.debug('loading nova modules')

    global __nova__
    __nova__ = NovaLazyLoader(_hubble_dir(), __opts__, __grains__, __pillar__, __salt__)

    ret = {'loaded': __nova__._dict.keys(),
           'missing': __nova__.missing_modules,
           'data': __nova__.__data__.keys(),
           'missing_data': __nova__.__missing_data__}
    return ret


def version():
    '''
    Report the version of this module
    '''
    return __version__


def _hubble_dir():
    '''
    Generate the local minion directories to which nova modules and profiles
    are synced

    Returns a tuple of two paths, the first for nova modules, the second for
    nova profiles
    '''
    nova_profile_dir = __salt__['config.get']('hubblestack:nova:profile_dir',
                                              'salt://hubblestack_nova_profiles')
    nova_module_dir = __salt__['config.get']('hubblestack:nova:module_dir',
                                             'salt://hubblestack_nova')
    dirs = []
    # Support optional salt:// in config
    for nova_dir in (nova_module_dir, nova_profile_dir):
        if 'salt://' in nova_dir:
            _, _, nova_dir = nova_dir.partition('salt://')
        saltenv = __salt__['config.get']('hubblestack:nova:saltenv', 'base')
        cachedir = os.path.join(__opts__.get('cachedir'),
                                'files',
                                saltenv,
                                nova_dir)
        dirs.append(cachedir)
    return tuple(dirs)


def _calculate_compliance(results):
    '''
    Calculate compliance numbers given the results of audits
    '''
    success = len(results.get('Success', []))
    failure = len(results.get('Failure', []))
    control = len(results.get('Controlled', []))
    total_audits = success + failure + control

    if total_audits:
        compliance = float(success + control)/total_audits
        compliance = int(compliance * 100)
        compliance = '{0}%'.format(compliance)
        return compliance
    return None


def _get_top_data(topfile):
    '''
    Helper method to retrieve and parse the nova topfile
    '''
    topfile = os.path.join(_hubble_dir()[1], topfile)

    try:
        with open(topfile) as handle:
            topdata = yaml.safe_load(handle)
    except Exception as e:
        raise CommandExecutionError('Could not load topfile: {0}'.format(e))

    if not isinstance(topdata, dict) or 'nova' not in topdata or \
            not(isinstance(topdata['nova'], dict)):
        raise CommandExecutionError('Nova topfile not formatted correctly')

    topdata = topdata['nova']

    ret = []

    for match, data in topdata.iteritems():
        if __salt__['match.compound'](match):
            ret.extend(data)

    return ret
