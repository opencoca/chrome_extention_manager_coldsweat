# -*- coding: utf-8 -*-
'''
Description: the feed fetcher

Copyright (c) 2013—2014 Andrea Peltrin
Portions are copyright (c) 2013 Rui Carmo
License: MIT (see LICENSE for details)
'''

import sys, re, time, cgi, urlparse, imp
from os import path
from datetime import datetime
from peewee import IntegrityError

import feedparser
import requests
from requests.exceptions import *

from models import *
from utilities import *
from filters import escape_html
from coldsweat import *
from markup import html

MAX_TITLE_LENGTH = 255
POSITIVE_STATUS_CODES = 200, 302, 304 # Other redirects are handled by Requests

# ------------------------------------------------------
# Entry data
# ------------------------------------------------------

def get_feed_timestamp(soup_feed, default):
    """
    Get the date a feed was last updated
    """
    for header in ['updated_parsed', 'published_parsed']:
        value = soup_feed.get(header, None)
        if value:
            # Fix future dates
            return min(tuple_as_datetime(value), default)
    logger.debug('no feed timestamp found, using default')    
    return default

def get_entry_timestamp(entry, default=None):
    """
    Select the best timestamp for an entry
    """
    for header in ['updated_parsed', 'published_parsed', 'created_parsed']:
        value = entry.get(header, None)
        if value:
            # Fix future dates
            return min(tuple_as_datetime(value), default)
    logger.debug('no entry timestamp found, using default')    
    return default
        
def get_entry_title(entry):
    if 'title' in entry:
        return truncate(html.strip_html(entry.title), MAX_TITLE_LENGTH)
    return 'Untitled'

def get_entry_link(entry):
    # Special case for Feedburner entries, see: http://bit.ly/1gRAvJv
    if 'feedburner_origlink' in entry:
        return entry.feedburner_origlink
    if 'link' in entry:
        return entry.link
    return None

    
def get_entry_id(entry, default=None):
    """
    Get a useful id from a feed entry
    """    
    if ('id' in entry) and entry.id: 
        return entry.id
    return default
    
def get_entry_author(entry, feed):
    """
    Divine authorship
    """

    if 'name' in entry.get('author_detail',[]):
        return entry.author_detail.name     
    elif 'name' in feed.get('author_detail', []):
        return feed.author_detail.name
    return None

def get_entry_content(entry):
    """
    Select the best content from an entry
    """

    candidates = entry.get('content', [])
    if candidates:
        logger.debug('content found for entry %s' % entry.link)    
    if 'summary_detail' in entry:
        logger.debug('summary found for entry %s' % entry.link)    
        candidates.append(entry.summary_detail)
    for c in candidates:
        if 'html' in c.type: # Match text/html, application/xhtml+xml
            return c.type, c.value
        else: 
            # If the content is declared to be (or is determined to be) text/plain, 
            #   it will not be sanitized by Feedparser. This is to avoid data loss.
            return c.type, escape_html(c.value)
    logger.debug('no content found for entry %s' % entry.link)    
    return 'text/plain', ''

# ------------------------------------------------------
# Add feed and subscription
# ------------------------------------------------------

def load_plugins():
    '''
    Load plugins listed in config file
    '''
    if not config.has_option('plugins', 'load'):
        return
        
    imports = config.get('plugins', 'load')
    for name in imports.split(','):
        name = name.strip()
        try:
            fp, pathname, description = imp.find_module(name, [plugin_dir])
            imp.load_module(name, fp, pathname, description)
        except ImportError, ex:
            logger.warn('could not load %s plugin (%s), ignored' % (name, ex))
            continue
        
        logger.debug('loaded %s plugin' % name)
        fp.close()
        
def add_feed(feed, fetch_icon=False, add_entries=False):
    '''
    Add a feed to database and optionally fetch icon and add entries
    '''

    # Normalize feed URL
    feed.self_link = scrub_url(feed.self_link)

    try:
        previous_feed = Feed.get(Feed.self_link == feed.self_link)
        logger.debug('feed %s has been already added to database, skipped' % feed.self_link)
        return previous_feed
    except Feed.DoesNotExist:
        pass

    if fetch_icon:
        # Prefer alternate_link if available since self_link could 
        #   point to Feed Burner or similar services
        icon_link = feed.alternate_link or feed.self_link    
        schema, netloc, path, query, fragment = urlparse.urlsplit(icon_link)
        icon = Icon.create(data=favicon.fetch(icon_link))
        feed.icon = icon
        logger.debug("saved favicon for %s: %s..." % (netloc, icon.data[:70]))    

    feed.save()
    fetch_feed(feed, add_entries)

    return feed
    
def add_subscription(feed, user, group):

    try:
        subscription = Subscription.create(user=user, feed=feed, group=group)
    except IntegrityError:
        logger.debug('user %s has already feed %s in her subscriptions' % (user.username, feed.self_link))    
        return None

    logger.debug('added feed %s for user %s' % (feed.self_link, user.username))                
    return subscription
    
# ------------------------------------------------------
# Feed fetching and parsing 
# ------------------------------------------------------

        
def fetch_url(url, timeout=None, etag=None, modified_since=None):

    request_headers = {
        'User-Agent': user_agent
    }

    # Conditional GET headers
    if etag and modified_since:
        request_headers['If-None-Match'] = etag
        request_headers['If-Modified-Since'] = format_http_datetime(modified_since)
        
    timeout = timeout if timeout else config.getint('fetcher', 'timeout')
    
    try:
        response = requests.get(url, timeout=timeout, headers=request_headers)
        logger.debug("got status %d" % response.status_code)
    except (IOError, RequestException), ex:
        return None
    
    return response


def fetch_feed(feed, add_entries=False):
    
    def post_fetch(status, error=False):
        if status:
            feed.last_status = status
        if error:
            feed.error_count = feed.error_count + 1        
        error_threshold = config.getint('fetcher', 'error_threshold')
        if error_threshold and (feed.error_count > error_threshold):
            feed.is_enabled = False
            feed.last_status = status # Save status code for posterity           
            logger.warn("%s has too many errors, disabled" % netloc)        
        feed.save()

    logger.debug("fetching %s" % feed.self_link)
           
    schema, netloc, path, params, query, fragment = urlparse.urlparse(feed.self_link)

    now = datetime.utcnow()

    interval = config.getint('fetcher', 'min_interval')

    # Check freshness
    for fieldname in ['last_checked_on', 'last_updated_on']:
        value = getattr(feed, fieldname)
        if not value:
            continue
        # No datetime.timedelta since we need to deal with large seconds values
        delta = datetime_as_epoch(now) - datetime_as_epoch(value)    
        if delta < interval:
            logger.debug("%s for %s is below min_interval, skipped" % (fieldname, netloc))
            return            
                      
    response = fetch_url(feed.self_link, etag=feed.etag, modified_since=feed.last_updated_on)
    if not response:
        # Record as "503 Service unavailable"
        post_fetch(503, error=True)
        logger.warn("a network error occured while fetching %s" % netloc)
        return

    feed.last_checked_on = now

    if response.history and response.history[0].status_code == 301:     # Moved permanently        
        self_link = response.url
        
        try:
            Feed.get(self_link=self_link)
        except Feed.DoesNotExist:
            feed.self_link = self_link                               
            logger.info("%s has changed its location, updated to %s" % (netloc, self_link))
        else:
            feed.is_enabled = False
            logger.warn("new %s location %s is duplicated, disabled" % (netloc, self_link))                
            post_fetch(DuplicatedFeedError.code)
            return

    if response.status_code == 304:                                     # Not modified
        logger.debug("%s hasn't been modified, skipped" % netloc)
        post_fetch(response.status_code)
        return
    elif response.status_code == 410:                                   # Gone
        logger.warn("%s is gone, disabled" % netloc)
        feed.is_enabled = False
        post_fetch(response.status_code)
        return
    elif response.status_code not in POSITIVE_STATUS_CODES:             # No good
        logger.warn("%s replied with status %d, aborted" % (netloc, response.status_code))
        post_fetch(response.status_code, error=True)
        return

    soup = feedparser.parse(response.text) 
    # Got parsing error? Log error but do not increment the error counter
    if hasattr(soup, 'bozo') and soup.bozo:
        logger.info("%s caused a parser error (%s), tried to parse it anyway" % (netloc, soup.bozo_exception))
        post_fetch(response.status_code, error=False)

    feed.etag = response.headers.get('ETag', None)    
    
    if 'link' in soup.feed:
        feed.alternate_link = soup.feed.link

    # Reset value only if not set before
    if ('title' in soup.feed) and not feed.title:
        feed.title = html.strip_html(soup.feed.title)

    feed.last_updated_on = get_feed_timestamp(soup.feed, now)        
    post_fetch(response.status_code)

    if not add_entries:    
        return
        
    for parsed_entry in soup.entries:
        
        link = get_entry_link(parsed_entry)
        guid = get_entry_id(parsed_entry, default=link)

        if not guid:
            logger.warn('could not find guid for entry from %s, skipped' % netloc)
            continue

        title                = get_entry_title(parsed_entry)
        mime_type, content   = get_entry_content(parsed_entry)
        timestamp            = get_entry_timestamp(parsed_entry, default=now)
        author               = get_entry_author(parsed_entry, soup.feed)
                
        # Skip ancient feed items        
        max_history = config.getint('fetcher', 'max_history')
        if max_history and ((now - timestamp).days > max_history):  
            logger.debug("entry %s from %s is over max_history, skipped" % (guid, netloc))
            continue

        try:
            # If entry is already in database with same id, then skip it
            Entry.get(guid=guid)
            logger.debug("duplicated entry %s, skipped" % guid)
            continue
        except Entry.DoesNotExist:
            pass

        entry = Entry(
            guid              = guid,
            feed              = feed,
            title             = title,
            author            = author,
            content           = content,
            #@@TODO: add mime_type too
            link              = link,
            last_updated_on   = timestamp
        )
        trigger_event('entry_parsed', entry, parsed_entry)
        entry.save()

        logger.debug(u"added entry %s from %s" % (guid, netloc))


def feed_worker(feed):

    if not feed.subscriptions:
        logger.debug("feed %s has no subscribers, skipped" % feed.self_link)
        return
            
    # Allow each process to open and close its database connection    
    connect()
    fetch_feed(feed, add_entries=True)
    close()
    
 
def fetch_feeds():
    """
    Fetch all feeds, possibly parallelizing requests
    """

    start = time.time()

    # Attach feed.subscriptions counter
    q = Feed.select(Feed, fn.Count(Subscription.user).alias('subscriptions')).join(Subscription, JOIN_LEFT_OUTER).group_by(Feed).where(Feed.is_enabled==True)
    
    feeds = list(q)
    if not feeds:
        logger.debug("no feeds found to refresh, halted")
        return

    load_plugins()

    logger.debug("starting fetcher")
    trigger_event('fetch_started')
        
    if config.getboolean('fetcher', 'multiprocessing'):
        from multiprocessing import Pool

        p = Pool(processes=None) # Uses cpu_count()        
        p.map(feed_worker, feeds)

    else:
        # Just sequence requests
        for feed in feeds:
            feed_worker(feed)
    
    trigger_event('fetch_done', feeds)
    
    logger.info("%d feeds checked in %.2fs" % (len(feeds), time.time() - start))




    
    