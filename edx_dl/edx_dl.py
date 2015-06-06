#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Main module for the edx-dl downloader.
It corresponds to the cli interface
"""

import argparse
import json
import os
import pickle
import sys

from functools import partial
from multiprocessing.dummy import Pool as ThreadPool

from six.moves.http_cookiejar import CookieJar
from six.moves.urllib.error import HTTPError, URLError
from six.moves.urllib.parse import urlencode
from six.moves.urllib.request import (
    urlopen,
    build_opener,
    install_opener,
    HTTPCookieProcessor,
    Request,
    urlretrieve,
)

from .common import YOUTUBE_DL_CMD, DEFAULT_CACHE_FILENAME, Unit
from .compat import compat_print
from .parsing import (
    edx_json2srt,
    extract_courses_from_html,
    extract_sections_from_html,
    extract_units_from_html,
)
from .utils import (
    clean_filename,
    directory_name,
    execute_command,
    get_filename_from_prefix,
    get_page_contents,
    get_page_contents_as_json,
    mkdir_p,
)


OPENEDX_SITES = {
    'edx': {
        'url': 'https://courses.edx.org',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'stanford': {
        'url': 'https://lagunita.stanford.edu',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'usyd-sit': {
        'url': 'http://online.it.usyd.edu.au',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'fun': {
        'url': 'https://www.france-universite-numerique-mooc.fr',
        'courseware-selector': ('section', {'aria-label': 'Menu du cours'}),
    },
    'gwu-seas': {
        'url': 'http://openedx.seas.gwu.edu',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'gwu-open': {
        'url': 'http://mooc.online.gwu.edu',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'mitprox': {
        'url': 'https://mitprofessionalx.mit.edu',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
}
BASE_URL = OPENEDX_SITES['edx']['url']
EDX_HOMEPAGE = BASE_URL + '/login_ajax'
LOGIN_API = BASE_URL + '/login_ajax'
DASHBOARD = BASE_URL + '/dashboard'
COURSEWARE_SEL = OPENEDX_SITES['edx']['courseware-selector']


def change_openedx_site(site_name):
    """
    Changes the openedx website for the given one via the key
    """
    global BASE_URL
    global EDX_HOMEPAGE
    global LOGIN_API
    global DASHBOARD
    global COURSEWARE_SEL

    if site_name not in OPENEDX_SITES.keys():
        compat_print("OpenEdX platform should be one of: %s" % ', '.join(OPENEDX_SITES.keys()))
        sys.exit(2)

    BASE_URL = OPENEDX_SITES[site_name]['url']
    EDX_HOMEPAGE = BASE_URL + '/login_ajax'
    LOGIN_API = BASE_URL + '/login_ajax'
    DASHBOARD = BASE_URL + '/dashboard'
    COURSEWARE_SEL = OPENEDX_SITES[site_name]['courseware-selector']


def _display_courses(courses):
    """
    List the courses that the user has enrolled.
    """
    compat_print('You can access %d courses' % len(courses))
    for i, course in enumerate(courses, 1):
        compat_print('%2d - %s [%s]' % (i, course.name, course.id))
        compat_print('     %s' % course.url)


def get_courses_info(url, headers):
    """
    Extracts the courses information from the dashboard.
    """
    page = get_page_contents(url, headers)
    courses = extract_courses_from_html(page, BASE_URL)
    return courses


def _get_initial_token(url):
    """
    Create initial connection to get authentication token for future
    requests.

    Returns a string to be used in subsequent connections with the
    X-CSRFToken header or the empty string if we didn't find any token in
    the cookies.
    """
    cookiejar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookiejar))
    install_opener(opener)
    opener.open(url)

    for cookie in cookiejar:
        if cookie.name == 'csrftoken':
            return cookie.value

    return ''


def get_available_sections(url, headers):
    """
    Extracts the sections and subsections from a given url
    """
    page = get_page_contents(url, headers)
    sections = extract_sections_from_html(page, BASE_URL)
    return sections


def edx_get_subtitle(url, headers):
    """
    Return a string with the subtitles content from the url or None if no
    subtitles are available.
    """
    try:
        json_object = get_page_contents_as_json(url, headers)
        return edx_json2srt(json_object)
    except URLError as exception:
        compat_print('[warning] edX subtitles (error:%s)' % exception.reason)
        return None
    except ValueError as exception:
        compat_print('[warning] edX subtitles (error:%s)' % exception.message)
        return None


def edx_login(url, headers, username, password):
    """
    logins user into the openedx website
    """
    post_data = urlencode({'email': username,
                           'password': password,
                           'remember': False}).encode('utf-8')
    request = Request(url, post_data, headers)
    response = urlopen(request)
    resp = json.loads(response.read().decode('utf-8'))
    return resp


def parse_args():
    """
    Parse the arguments/options passed to the program on the command line.
    """
    parser = argparse.ArgumentParser(prog='edx-dl',
                                     description='Get videos from the OpenEdX platform',
                                     epilog='For further use information,'
                                     'see the file README.md',)
    # positional
    parser.add_argument('course_urls',
                        nargs='*',
                        action='store',
                        default=[],
                        help='target course urls'
                        '(e.g., https://courses.edx.org/courses/BerkeleyX/CS191x/2013_Spring/info)')

    # optional
    parser.add_argument('-u',
                        '--username',
                        required=True,
                        action='store',
                        help='your edX username (email)')
    parser.add_argument('-p',
                        '--password',
                        required=True,
                        action='store',
                        help='your edX password')
    parser.add_argument('-f',
                        '--format',
                        dest='format',
                        action='store',
                        default=None,
                        help='format of videos to download')
    parser.add_argument('-s',
                        '--with-subtitles',
                        dest='subtitles',
                        action='store_true',
                        default=False,
                        help='download subtitles with the videos')
    parser.add_argument('-o',
                        '--output-dir',
                        action='store',
                        dest='output_dir',
                        help='store the files to the specified directory',
                        default='Downloaded')
    parser.add_argument('-x',
                        '--platform',
                        action='store',
                        dest='platform',
                        help='OpenEdX platform, currently either "edx", "stanford" or "usyd-sit"',
                        default='edx')
    parser.add_argument('--list-courses',
                        dest='list_courses',
                        action='store_true',
                        default=False,
                        help='list available courses')
    parser.add_argument('--filter-section',
                        dest='filter_section',
                        action='store',
                        default=None,
                        help='filters sections to be downloaded')
    parser.add_argument('--list-sections',
                        dest='list_sections',
                        action='store_true',
                        default=False,
                        help='list available sections')
    parser.add_argument('--youtube-options',
                        dest='youtube_options',
                        action='store',
                        default='',
                        help='list available courses without downloading')
    parser.add_argument('--prefer-cdn-videos',
                        dest='prefer_cdn_videos',
                        action='store_true',
                        default=False,
                        help='prefer CDN video downloads over youtube (BETA)')
    parser.add_argument('--cache',
                        dest='cache',
                        action='store_true',
                        default=False,
                        help='uses cache to avoid reparsing already extracted items')
    parser.add_argument('--dry-run',
                        dest='dry_run',
                        action='store_true',
                        default=False,
                        help='makes a dry run, only lists the resources')
    args = parser.parse_args()
    return args


def edx_get_headers():
    """
    Builds the openedx headers to create requests
    """
    headers = {
        'User-Agent': 'edX-downloader/0.01',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8',
        'Referer': EDX_HOMEPAGE,
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': _get_initial_token(EDX_HOMEPAGE),
    }
    return headers


def extract_units(url, headers):
    """
    Parses a webpage and extracts its resources e.g. video_url, sub_url, etc.
    """
    compat_print("Processing '%s'" % url)
    page = get_page_contents(url, headers)
    units = extract_units_from_html(page, BASE_URL)
    return units


def extract_all_units(urls, headers):
    """
    Returns a dict of all the units in the selected_sections: {url, units}
    """
    # for development purposes you may want to uncomment this line
    # to test serial execution, and comment all the pool related ones
    # units = [extract_units(url, headers) for url in urls]
    mapfunc = partial(extract_units, headers=headers)
    pool = ThreadPool(20)
    units = pool.map(mapfunc, urls)
    pool.close()
    pool.join()

    all_units = dict(zip(urls, units))
    return all_units


def _display_sections_menu(course, sections):
    """
    List the weeks for the given course.
    """
    num_sections = len(sections)
    compat_print('%s [%s] has %d sections so far' % (course.name, course.id, num_sections))
    for i, section in enumerate(sections, 1):
        compat_print('%2d - Download %s videos' % (i, section.name))


def _filter_sections(index, sections):
    """
    Get the sections for the given index, if the index is not valid chooses all
    """
    num_sections = len(sections)
    if index is not None:
        try:
            index = int(index)
            if index > 0 and index <= num_sections:
                return [sections[index - 1]]
        except ValueError:
            pass
    return sections


def _display_sections(sections):
    """
    Displays a tree of section(s) and subsections
    """
    compat_print('Downloading %d section(s)' % len(sections))
    for section in sections:
        compat_print('Section %2d: %s' % (section.position, section.name))
        for subsection in section.subsections:
            compat_print('  %s' % subsection.name)


def parse_courses(args, available_courses):
    """
    Parses courses options and returns the selected_courses
    """
    if args.list_courses:
        _display_courses(available_courses)
        exit(0)

    if len(args.course_urls) == 0:
        compat_print('You must pass the URL of at least one course, check the correct url with --list-courses')
        exit(3)

    selected_courses = [available_course
                        for available_course in available_courses
                        for url in args.course_urls
                        if available_course.url == url]
    if len(selected_courses) == 0:
        compat_print('You have not passed a valid course url, check the correct url with --list-courses')
        exit(4)
    return selected_courses


def parse_sections(args, selections):
    """
    Parses sections options and returns selections filtered by
    selected_sections
    """
    if args.list_sections:
        for selected_course, selected_sections in selections.items():
            _display_sections_menu(selected_course, selected_sections)
        exit(0)

    if not args.filter_section:
        return selections

    filtered_selections = {selected_course:
                           _filter_sections(args.filter_section, selected_sections)
                           for selected_course, selected_sections in selections.items()}
    return filtered_selections


def _display_selections(selections):
    """
    Displays the course, sections and subsections to be downloaded
    """
    for selected_course, selected_sections in selections.items():
        compat_print('Downloading %s [%s]' % (selected_course.name,
                                              selected_course.id))
        _display_sections(selected_sections)


def parse_units(all_units):
    """
    Parses units options and corner cases
    """
    flat_units = [unit for units in all_units.values() for unit in units]
    if len(flat_units) < 1:
        compat_print('WARNING: No downloadable video found.')
        exit(6)


def _download_video_youtube(unit, args, target_dir, filename_prefix):
    """
    Downloads the url in unit.video_youtube_url using youtube-dl
    """
    if unit.video_youtube_url is not None:
        filename = filename_prefix + "-%(title)s-%(id)s.%(ext)s"
        fullname = os.path.join(target_dir, filename)
        video_format_option = args.format + '/mp4' if args.format else 'mp4'

        cmd = YOUTUBE_DL_CMD + ['-o', fullname, '-f',
                                video_format_option]
        if args.subtitles:
            cmd.append('--all-subs')
        cmd.extend(args.youtube_options.split())
        cmd.append(unit.video_youtube_url)
        execute_command(cmd)


def get_subtitles_download_urls(available_subs_url, sub_template_url, headers):
    """
    Request the available subs and builds the urls to download subs
    """
    if available_subs_url is not None and sub_template_url is not None:
        try:
            available_subs = get_page_contents_as_json(available_subs_url,
                                                       headers)
        except HTTPError:
            available_subs = ['en']

        return {sub_lang: sub_template_url % sub_lang
                for sub_lang in available_subs}
    return {}


def _download_subtitles(unit, target_dir, filename_prefix, headers):
    """
    Downloads the subtitles using the openedx subtitle api
    """
    filename = get_filename_from_prefix(target_dir, filename_prefix)
    if filename is None:
        compat_print('[warning] no video downloaded for %s' % filename_prefix)
        return
    if unit.sub_template_url is None:
        compat_print('[warning] no subtitles downloaded for %s' % filename_prefix)
        return

    subtitles_download_urls = get_subtitles_download_urls(unit.available_subs_url,
                                                          unit.sub_template_url,
                                                          headers)
    for sub_lang, sub_url in subtitles_download_urls.items():
        subs_filename = os.path.join(target_dir,
                                     filename + '.' + sub_lang + '.srt')
        if not os.path.exists(subs_filename):
            subs_string = edx_get_subtitle(sub_url, headers)
            if subs_string:
                compat_print('[info] Writing edX subtitle: %s' % subs_filename)
                open(os.path.join(os.getcwd(), subs_filename),
                     'wb+').write(subs_string.encode('utf-8'))
        else:
            compat_print('[info] Skipping existing edX subtitle %s' % subs_filename)


def download_urls(urls, target_dir, filename_prefix):
    """
    Downloads urls in target_dir and adds the filename_prefix to each filename
    """
    for url in urls:
        original_filename = url.rsplit('/', 1)[1]
        filename = os.path.join(target_dir,
                                filename_prefix + '-' + original_filename)
        compat_print('[download] Destination: %s' % filename)
        urlretrieve(url, filename)


def download_unit(unit, args, target_dir, filename_prefix, headers):
    """
    Downloads unit based on args in the given target_dir with filename_prefix
    """
    if args.prefer_cdn_videos:
        download_urls(unit.mp4_urls, target_dir, filename_prefix)
        # FIXME: get out of the conditions once the proper downloader is ready
        download_urls(unit.resources_urls, target_dir, filename_prefix)
    else:
        _download_video_youtube(unit, args, target_dir, filename_prefix)

    if args.subtitles:
        _download_subtitles(unit, target_dir, filename_prefix, headers)


def download(args, selections, all_units, headers):
    """
    Downloads all the resources based on the selections
    """
    compat_print("[info] Output directory: " + args.output_dir)
    # Download Videos
    # notice that we could iterate over all_units, but we prefer to do it over
    # sections/subsections to add correct prefixes and shows nicer information
    for selected_course, selected_sections in selections.items():
        coursename = directory_name(selected_course.name)
        for selected_section in selected_sections:
            section_dirname = "%02d-%s" % (selected_section.position,
                                           selected_section.name)
            target_dir = os.path.join(args.output_dir, coursename,
                                      clean_filename(section_dirname))
            mkdir_p(target_dir)
            counter = 0
            for subsection in selected_section.subsections:
                units = all_units.get(subsection.url, [])
                for unit in units:
                    counter += 1
                    filename_prefix = "%02d" % counter
                    download_unit(unit, args, target_dir, filename_prefix,
                                  headers)


def remove_repeated_urls(all_units):
    """
    Removes repeated urls from the units, it does not consider subtitles.
    This is done to avoid repeated downloads
    """
    existing_urls = set()
    filtered_units = {}
    for url, units in all_units.items():
        reduced_units = []
        for unit in units:
            # we don't analyze the subtitles for repetition since
            # their size is negligible for the goal of this function
            video_youtube_url = None
            if unit.video_youtube_url not in existing_urls:
                video_youtube_url = unit.video_youtube_url
                existing_urls.add(unit.video_youtube_url)
            mp4_urls = []
            for mp4_url in unit.mp4_urls:
                if mp4_url not in existing_urls:
                    mp4_urls.append(mp4_url)
                    existing_urls.add(mp4_url)
            resources_urls = []
            for resource_url in unit.resources_urls:
                if resource_url not in existing_urls:
                    resources_urls.append(resource_url)
                    existing_urls.add(resource_url)

            if video_youtube_url is not None or len(mp4_urls) > 0 or len(resources_urls) > 0:
                reduced_units.append(Unit(video_youtube_url=video_youtube_url,
                                          available_subs_url=unit.available_subs_url,
                                          sub_template_url=unit.sub_template_url,
                                          mp4_urls=mp4_urls,
                                          resources_urls=resources_urls))
        filtered_units[url] = reduced_units
    return filtered_units


def num_urls_in_units_dict(units_dict):
    """
    Counts the number of urls in a all_units dict, it ignores subtitles from its
    counting
    """
    return sum((1 if unit.video_youtube_url is not None else 0) +
               (1 if unit.available_subs_url is not None else 0) +
               (1 if unit.sub_template_url is not None else 0) +
               len(unit.mp4_urls) + len(unit.resources_urls)
               for units in units_dict.values() for unit in units)


def extract_all_units_with_cache(filename, all_urls, headers):
    """
    Extracts the units who are not in the cache (filename) and returns
    The full list of units (cached+new)
    """
    cached_units = {}
    if os.path.exists(filename):
        with open(filename, 'rb') as f:
            cached_units = pickle.load(f)

    # we filter the cached urls
    new_urls = [url for url in all_urls if url not in cached_units]
    compat_print('loading %d urls from cache [%s]' % (len(cached_units.keys()),
                                                      filename))
    new_units = extract_all_units(new_urls, headers)
    all_units = cached_units.copy()
    all_units.update(new_units)
    return all_units


def write_units_to_cache(filename, units):
    """
    writes units to cache
    """
    compat_print('writing %d urls to cache [%s]' % (len(units.keys()),
                                                    filename))
    with open(filename, 'wb') as f:
        pickle.dump(units, f)


def main():
    """
    Main program function
    """
    args = parse_args()

    change_openedx_site(args.platform)

    if not args.username or not args.password:
        compat_print("You must supply username and password to log-in")
        exit(1)

    # Prepare Headers
    headers = edx_get_headers()

    # Login
    resp = edx_login(LOGIN_API, headers, args.username, args.password)
    if not resp.get('success', False):
        compat_print(resp.get('value', "Wrong Email or Password."))
        exit(2)

    # Parse and select the available courses
    courses = get_courses_info(DASHBOARD, headers)
    available_courses = [course for course in courses if course.state == 'Started']
    selected_courses = parse_courses(args, available_courses)

    # Parse the sections and build the selections dict filtered by sections
    all_selections = {selected_course:
                      get_available_sections(selected_course.url.replace('info', 'courseware'), headers)
                      for selected_course in selected_courses}
    selections = parse_sections(args, all_selections)
    _display_selections(selections)

    # Extract the unit information (downloadable resources)
    # This parses the HTML of all the subsection.url and extracts
    # the URLs of the resources as Units.
    all_urls = [subsection.url
                for selected_sections in selections.values()
                for selected_section in selected_sections
                for subsection in selected_section.subsections]

    if args.cache:
        all_units = extract_all_units_with_cache(DEFAULT_CACHE_FILENAME,
                                                 all_urls, headers)
    else:
        all_units = extract_all_units(all_urls, headers)

    parse_units(selections)

    if args.cache:
        write_units_to_cache(DEFAULT_CACHE_FILENAME, all_units)

    # This removes all repeated important urls
    # FIXME: This is not the best way to do it but it is the simplest, a
    # better approach will be to create symbolic or hard links for the repeated
    # units to avoid losing information
    filtered_units = remove_repeated_urls(all_units)
    num_all_urls = num_urls_in_units_dict(all_units)
    num_filtered_urls = num_urls_in_units_dict(filtered_units)
    compat_print('Removed %d duplicated urls from %d in total' %
                 ((num_all_urls - num_filtered_urls), num_all_urls))

    # finally we download all the resources
    if not args.dry_run:
        download(args, selections, all_units, headers)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        compat_print("\n\nCTRL-C detected, shutting down....")
        sys.exit(0)
