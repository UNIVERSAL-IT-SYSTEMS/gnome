#!/usr/bin/env python2
# vim: set sw=4 sts=4 et :
# Author(s): Nirbheek Chauhan <nirbheek@gentoo.org>
#
# Given a category/package list, and (optionally) a new/old release number,
# generates a STABLEREQ list, or a KEYWORDREQ list.
#
# Toggle STABLE to control which type of list to generate.
#
# You can use test-data/package-list to test the script out.
#
# NOTE: This script assumes that there are no broken keyword deps
#
# BUGS:
# * belongs_release() is a very primitive function, which means usage of
#   old/new release numbers gives misleading output
# * Will show multiple versions of the same package in the output sometimes.
#   This happens when a cp is specified in the cpv list, and is resolved as
#   a dependency as well.
# TODO:
# * Support recursive checking of needed keywords in deps
#

from __future__ import division

import argparse
import collections
import os
import sys

import portage

#############
# Constants #
#############
# GNOME_OVERLAY = PORTDB.getRepositoryPath('gnome')
portage.portdb.porttrees = [portage.settings['PORTDIR']]
STABLE_ARCHES = ('alpha', 'amd64', 'arm', 'hppa', 'ia64', 'm68k', 'ppc',
                 'ppc64', 's390', 'sh', 'sparc', 'x86')
UNSTABLE_ARCHES = ('~alpha', '~amd64', '~arm', '~hppa', '~ia64', '~m68k',
                   '~ppc', '~ppc64', '~s390', '~sh', '~sparc', '~x86',
                   '~x86-fbsd')
ALL_ARCHES = STABLE_ARCHES + UNSTABLE_ARCHES
SYSTEM_PACKAGES = []

############
# Settings #
############
DEBUG = False
EXTREME_DEBUG = False
CHECK_DEPS = False
APPEND_SLOTS = False
# Check for stable keywords
# This is intended to switch between keywordreq (for ~arch)
# and stablereq (for moving from ~arch to arch)
STABLE = True

# if not STABLE:
#     print 'Currently broken for anything except STABLEREQ'
#     print 'Please set STABLE to True'
#     sys.exit(1)

###############
# Preparation #
###############

ARCHES = None
if STABLE:
    ARCHES = STABLE_ARCHES
else:
    ARCHES = UNSTABLE_ARCHES


####################
# Define Functions #
####################
def flatten(list, sep=' '):
    "Given a list, returns a flat string separated by 'sep'"
    return sep.join(list)


def n_sep(n, sep=' '):
    tmp = ''
    for i in range(0, n):
        tmp += sep
    return tmp


def debug(*strings):
    from portage.output import EOutput
    ewarn = EOutput().ewarn
    ewarn(flatten(strings))


def nothing_to_be_done(atom, type='cpv'):
    if STABLE:
        debug('%s %s: already stable, ignoring...' % (type, atom))
    else:
        debug('%s %s: already keyworded, ignoring...' % (type, atom))


def make_unstable(kws):
    """Transform `kws` into a list of unstable keywords."""
    return set([
        kwd if kwd.startswith('~') else '~' + kwd
        for kwd in kws
    ])


def belongs_release(cpv, release):
    """Check if `cpv` belongs to the release `release`."""
    # FIXME: This failure function needs better logic
    if CHECK_DEPS:
        raise Exception('This function is utterly useless with RECURSIVE mode')
    return portage.versions.cpv_getversion(cpv).startswith(release)


def issystempackage(cpv):
    for i in SYSTEM_PACKAGES:
        if cpv.startswith(i):
            return True
    return False


def get_kws(cpv, arches=ARCHES):
    """Return keywords of `cpv` filtered by `arches`."""
    return set([
        kwd for kwd in portage.portdb.aux_get(cpv, ['KEYWORDS'])[0].split()
        if kwd in arches
    ])


def can_stabilize_cpv(cpv, release=None):
    """Whether `cpv` matches stabilization criteria.

    `cpv` must:
    * belong to the release
    * not be p.masked
    * have keywords
    """
    if release and not belongs_release(cpv, release):
        return False
    if not portage.portdb.visible([cpv]):
        return False
    if not get_kws(cpv, arches=ALL_ARCHES):
        return False
    return True


def match_wanted_atoms(atom, release=None):
    """Return a list of CPV matching `atom`.

    If `release` is provided, CPVs are filtered against it.

    The list is sorted by descending order of version.
    """
    # xmatch is stupid, and ignores ! in an atom...
    if atom.startswith('!'):
        return []

    return [
        cpv for cpv in reversed(portage.portdb.xmatch('match-all', atom))
        if can_stabilize_cpv(cpv, release)
    ]


def get_best_deps(cpv, kws, release=None):
    """
    Returns a list of the best deps of a cpv, optionally matching a release,
    and with max of the specified keywords
    """
    atoms = portage.portdb.aux_get(cpv, ['DEPEND', 'RDEPEND', 'PDEPEND'])
    atoms = ' '.join(atoms).split()  # consolidate atoms
    atoms = list(set(atoms))  # de-duplicate
    deps = set()
    tmp = []
    for atom in atoms:
        if atom.find('/') is -1:
            # It's not a dep atom
            continue
        ret = match_wanted_atoms(atom, release)
        if not ret:
            if DEBUG:
                debug('We encountered an irrelevant atom: %s' % atom)
            continue
        best_kws = ['', []]
        for i in ret:
            if STABLE:
                # Check that this version has unstable keywords
                ukws = make_unstable(kws)
                cur_ukws = make_unstable(get_kws(i, arches=kws | ukws))
                if cur_ukws.intersection(ukws) != ukws:
                    best_kws = 'none'
                    if DEBUG:
                        debug('Insufficient unstable keywords in: %s' % i)
                    continue
            cur_match_kws = get_kws(i, arches=kws)
            if cur_match_kws == kws:
                # This dep already has all keywords
                best_kws = 'alreadythere'
                break
            # Select the version which needs least new keywords
            if len(cur_match_kws) > len(best_kws[1]):
                best_kws = [i, cur_match_kws]
            elif not best_kws[0]:
                # This means that none of the versions have any of the stable
                # keywords that *we checked* (i.e. kws).
                best_kws = [i, []]
        if best_kws == 'alreadythere':
            if DEBUG:
                nothing_to_be_done(atom, type='dep')
            continue
        elif best_kws == 'none':
            continue
        elif not best_kws[0]:
            # We get this when the if STABLE: block above rejects everything.
            # This means that this atom does not have any versions with
            # unstable keywords matching the unstable keywords of the cpv
            # that pulls it.
            # This mostly happens because an || or use dep exists. However, we
            # make such deps strict while parsing
            # XXX: We arbitrarily select the most recent version for this case
            deps.add(ret[0])
        elif not best_kws[1]:
            # This means that none of the versions have any of the stable
            # keywords that *we checked* (i.e. kws). Hence, we do another pass;
            # this time checking *all* keywords.
            #
            # XXX: We duplicate some of the things from the for loop above
            # We don't need to duplicate anything that caused a 'continue' or
            # a 'break' above
            ret = match_wanted_atoms(atom, release)
            best_kws = ['', []]
            for i in ret:
                cur_kws = get_kws(i)
                if len(cur_kws) > len(best_kws[1]):
                    best_kws = [i, cur_kws]
                elif not best_kws[0]:
                    # This means that none of the versions have any of
                    # the stable keywords *at all*. No choice but to
                    # arbitrarily select the latest version in that case.
                    best_kws = [i, []]
            deps.add(best_kws[0])
        else:
            deps.add(best_kws[0])
    return list(deps)


def max_kws(cpv, release=None):
    """Build `cpv` maximum expected keyword coverage.

    Find the intersection of "most keywords it can have" and
    "keywords it has", and returns a sorted list

    If STABLE; makes sure it has unstable keywords right now

    Returns [] if current cpv has best keywords
    Returns None if no cpv has keywords
    """
    current_kws = set(get_kws(cpv, arches=ALL_ARCHES))
    maximum_kws = set()  # Maximum keywords that a cpv has
    missing_kws = set()

    # Build best keyword coverage for `cpv`
    for atom in match_wanted_atoms('<=' + cpv, release):
        kws = get_kws(atom)

        # Consider stable keywords only
        if STABLE:
            kws = [kwd for kwd in kws if not kwd.startswith('~')]

        maximum_kws.update(set(kws))

    # Build list of keywords missing to achieve best coverage
    for kwd in maximum_kws:
        # Skip stable keywords with no corresponding unstable keyword in `cpv`
        if STABLE and '~' + kwd not in current_kws:
            continue
        missing_kws.add(kwd)

    if maximum_kws:
        return missing_kws
    else:
        # No cpv has the keywords we need
        return None


# FIXME: This is broken
def kws_wanted(current_kws, target_kws):
    """Generate a list of kws that need to be updated."""
    wanted = set()
    for kwd in target_kws:
        if STABLE and '~' + kwd not in current_kws:
            # Skip stable keywords with no corresponding unstable keyword
            continue
        wanted.add(kwd)
    return wanted


def gen_cpv_kws(cpv, kws_aim, depgraph, check_dependencies, new_release):
    """Build a list of CPV-Keywords.

    If `check_dependencies` is True, append dependencies that need to be
    updated to the list.
    """
    wanted = kws_wanted(get_kws(cpv, arches=ALL_ARCHES), kws_aim)

    if not wanted:
        # This happens when cpv has less keywords than kws_aim
        # Usually happens when a dep was an || dep, or under a USE-flag
        # which is masked in some profiles. We make all deps strict in
        # get_best_deps()
        # So... let's just stabilize it on all arches we can, and ignore for
        # keywording since we have no idea about that.
        if not STABLE:
            debug('MEH')
            nothing_to_be_done(cpv, type='dep')
            return None

        wanted = get_kws(cpv, arches=make_unstable(kws_aim))

    cpv_kw_list = [(cpv, wanted)]

    if check_dependencies and not issystempackage(cpv):
        deps = get_best_deps(cpv, wanted, release=new_release)
        if EXTREME_DEBUG:
            debug('The deps of %s are %s' % (cpv, deps))

        for dep in deps:
            if dep in depgraph:
                # XXX: assumes that `kws_aim` of previously added cpv is
                #      larger than current
                continue

            depgraph.add(dep)
            # XXX: Assumes that dependencies are keyworded the same than cpv
            dep_deps = gen_cpv_kws(dep, wanted, depgraph, check_dependencies,
                                   new_release)
            dep_deps.reverse()

            for cpv_kw_tuple in dep_deps:
                # Make sure we don't already have the same [(cpv, kws)]
                if cpv_kw_tuple not in cpv_kw_list:
                    cpv_kw_list.append(cpv_kw_tuple)

    cpv_kw_list.reverse()
    return cpv_kw_list


def consolidate_dupes(cpv_kws):
    """Consolidate duplicate CPVs with differing keywords.

    Cannot handle CPs with different versions since we don't know if they are
    inter-changeable.
    """
    # Build maximum requested keywords for each cpv
    cpv_kws_dict = collections.defaultdict(set)
    for dep_set in cpv_kws:
        for cpv, kws in dep_set:
            cpv_kws_dict[cpv].update(kws)

    # Update cpv with their maximum request keywords
    clean_cpv_kws = []
    for dep_set in cpv_kws:
        clean_cpv_kws.append([
            (cpv, cpv_kws_dict.pop(cpv))
            # Keep only first occurence of cpv
            for cpv, _ in dep_set if cpv in cpv_kws_dict
        ])

    return clean_cpv_kws


def get_per_slot_cpvs(cpvs):
    "Classify the given cpvs into slots, and yield the best atom for each slot"
    slots = set()
    for cpv in cpvs:
        slot = portage.portage.portdb.aux_get(cpv, ['SLOT'])[0]
        if slot in slots:
            continue
        slots.add(slot)
        yield cpv


def append_slots(cpv_kws):
    "Append slots at the end of cpv atoms"
    slotifyed_cpv_kws = []
    for (cpv, kws) in cpv_kws:
        slot = portage.portage.portdb.aux_get(cpv, ['SLOT'])[0]
        cpv = "%s:%s" % (cpv, slot)
        slotifyed_cpv_kws.append([cpv, kws])
    return slotifyed_cpv_kws


# FIXME: This is broken
def prettify(cpv_kws):
    "Takes a list of [cpv, [kws]] and prettifies it"
    max_len = 0
    kws_all = []
    pretty_list = []
    cpv_block_size = 0

    for each in cpv_kws:
        # Ignore comments/whitespace carried over from original list
        if type(each) is not list:
            continue
        # Find the atom with max length (for output formatting)
        if len(each[0]) > max_len:
            max_len = len(each[0])
        # Find the set of all kws listed
        for kw in each[1]:
            if kw not in kws_all:
                kws_all.append(kw)
    kws_all.sort()

    for each in cpv_kws:
        # Handle comments/whitespace carried over from original list
        if type(each) is not list:
            # If the prev cpv block has just one line, don't print an extra \n
            # XXX: This code relies on blocks of dep-cpvs being separated by \n
            if CHECK_DEPS and cpv_block_size is 1:
                cpv_block_size = 0
                continue
            pretty_list.append([each, []])
            cpv_block_size = 0
            continue
        # The size of the current cpv list block
        cpv_block_size += 1
        # Pad the cpvs with space
        each[0] += n_sep(max_len - len(each[0]))
        for i in range(0, len(kws_all)):
            if i == len(each[1]):
                # Prevent an IndexError
                # This is a problem in the algo I selected
                each[1].append('')
            if each[1][i] != kws_all[i]:
                # If no arch, insert space
                each[1].insert(i, n_sep(len(kws_all[i])))
        pretty_list.append([each[0], each[1]])
    return pretty_list


#####################
# Use the Functions #
#####################
# cpvs that will make it to the final list
def main():
    """Where the magic happens!"""
    parser = argparse.ArgumentParser(
        description='Generate a stabilization request for multiple packages'
    )
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='Make output more verbose')
    parser.add_argument('--extreme-debug', action='store_true', default=False,
                        help='Make output even more verbose')
    parser.add_argument('--check-dependencies',
                        action='store_true', default=False,
                        help='Check dependencies are keyworded and if not,'
                             ' add them to the list')
    parser.add_argument('--append-slots', action='store_true', default=False,
                        help='Append slots to CPVs output')
    parser.add_argument('file', help='File to read CP from')
    parser.add_argument('old_version', nargs='?',
                        help='An optional argument specifying which release'
                             ' cycle to use to get CPVs which has the'
                             ' reference keywords for stabilization.')
    parser.add_argument('new_version', nargs='?',
                        help='An optional argument specifying which release'
                             ' cycle to use to get the latest CPVs that needs'
                             ' to be stabilized')
    args = parser.parse_args()

    ALL_CPV_KWS = []
    for i in open(args.file).readlines():
        cp = i[:-1]
        if cp.startswith('#') or cp.isspace() or not cp:
            ALL_CPV_KWS.append(cp)
            continue
        if cp.find('#') is not -1:
            raise Exception('Inline comments are not supported')
        if portage.catpkgsplit(cp):
            # cat/pkg is already a categ/pkg-ver
            cpvs = [cp]
        else:
            # Get all the atoms matching the given cp
            cpvs = match_wanted_atoms(cp, release=args.new_version)

        for cpv in get_per_slot_cpvs(cpvs):
            if not cpv:
                debug('%s: Invalid cpv' % cpv)
                continue

            kws_missing = max_kws(cpv, release=args.old_version)
            if kws_missing is None:
                debug('No versions with stable keywords for %s' % cpv)
                # No cpv with stable keywords => select latest
                arches = make_unstable(ARCHES)
                kws_missing = [kw[1:] for kw in get_kws(cpv, arches)]

            elif not kws_missing:
                # Current cpv has the max keywords => nothing to do
                nothing_to_be_done(cpv)
                continue

            ALL_CPV_KWS.append(
                gen_cpv_kws(cpv, kws_missing, set([cpv]),
                            args.check_dependencies, args.new_version)
            )

    ALL_CPV_KWS = consolidate_dupes(ALL_CPV_KWS)
    if args.append_slots:
        ALL_CPV_KWS = append_slots(ALL_CPV_KWS)

    for i in prettify(ALL_CPV_KWS):
        print i[0], flatten(i[1])


if __name__ == '__main__':
    main()
