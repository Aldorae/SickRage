#
# This file is part of Sick Beard.
#
# Sick Beard is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Sick Beard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Sick Beard.  If not, see <http://www.gnu.org/licenses/>.
#
# Created on Sep 20, 2012
# @author: Dermot Buckley <dermot@buckley.ie>
# @copyright: Dermot Buckley
#

import time
import traceback

try:
    import json
except ImportError:
    from lib import simplejson as json

from sickbeard import logger
from sickbeard import db
from sickbeard.helpers import getURL
from sickbeard.exceptions import ex
from lib import requests

MAX_XEM_AGE_SECS = 86400 # 1 day

_schema_created = False
def _check_for_schema():
    global _schema_created
    if not _schema_created:
        myDB = db.DBConnection()
        cacheDB = db.DBConnection('cache.db')
        myDB.action('CREATE TABLE if not exists scene_numbering (indexer_id INTEGER, season INTEGER, episode INTEGER, scene_season INTEGER, scene_episode INTEGER, PRIMARY KEY (indexer_id, season, episode))')
        
        cacheDB.action('CREATE TABLE if not exists xem_numbering (indexer_id INTEGER, season INTEGER, episode INTEGER, scene_season INTEGER, scene_episode INTEGER, PRIMARY KEY (indexer_id, season, episode))')
        cacheDB.action('CREATE TABLE if not exists xem_refresh (indexer_id INTEGER PRIMARY KEY, last_refreshed INTEGER)')
        _schema_created = True
        
def get_scene_numbering(indexer_id, season, episode, fallback_to_xem=True):
    """
    Returns a tuple, (season, episode), with the scene numbering (if there is one),
    otherwise returns the xem numbering (if fallback_to_xem is set), otherwise 
    returns the tvdb numbering.
    (so the return values will always be set)
    
    @param indexer_id: int
    @param season: int
    @param episode: int
    @param fallback_to_xem: bool If set (the default), check xem for matches if there is no local scene numbering
    @return: (int, int) a tuple with (season, episode)   
    """
    if indexer_id is None or season is None or episode is None:
        return (season, episode)
    
    result = find_scene_numbering(indexer_id, season, episode)
    if result:
        return result
    else:
        if fallback_to_xem:
            xem_result = find_xem_numbering(indexer_id, season, episode)
            if xem_result:
                return xem_result
        return (season, episode)
    
def find_scene_numbering(indexer_id, season, episode):
    """
    Same as get_scene_numbering(), but returns None if scene numbering is not set
    """
    if indexer_id is None or season is None or episode is None:
        return (season, episode)
    
    _check_for_schema()
    myDB = db.DBConnection()
        
    rows = myDB.select("SELECT scene_season, scene_episode FROM scene_numbering WHERE indexer_id = ? and season = ? and episode = ?", [indexer_id, season, episode])
    if rows:
        return (int(rows[0]["scene_season"]), int(rows[0]["scene_episode"]))
    else:
        return (season, episode)
    
def get_indexer_numbering(indexer_id, sceneSeason, sceneEpisode, fallback_to_xem=True):
    """
    Returns a tuple, (season, episode) with the tvdb numbering for (sceneSeason, sceneEpisode)
    (this works like the reverse of get_scene_numbering)
    """
    if indexer_id is None or sceneSeason is None or sceneEpisode is None:
        return (sceneSeason, sceneEpisode)
    
    _check_for_schema()
    myDB = db.DBConnection()
        
    rows = myDB.select("SELECT season, episode FROM scene_numbering WHERE indexer_id = ? and scene_season = ? and scene_episode = ?", [indexer_id, sceneSeason, sceneEpisode])
    if rows:
        return (int(rows[0]["season"]), int(rows[0]["episode"]))
    else:
        if fallback_to_xem:
            return get_indexer_numbering_for_xem(indexer_id, sceneSeason, sceneEpisode)
        return (sceneSeason, sceneEpisode)
    
def get_scene_numbering_for_show(indexer_id):
    """
    Returns a dict of (season, episode) : (sceneSeason, sceneEpisode) mappings
    for an entire show.  Both the keys and values of the dict are tuples.
    Will be empty if there are no scene numbers set
    """
    if indexer_id is None:
        return {}
    
    _check_for_schema()
    myDB = db.DBConnection()
        
    rows = myDB.select('''SELECT season, episode, scene_season, scene_episode 
                        FROM scene_numbering WHERE indexer_id = ?
                        ORDER BY season, episode''', [indexer_id])
    result = {}
    for row in rows:
        result[(int(row['season']), int(row['episode']))] = (int(row['scene_season']), int(row['scene_episode']))
        
    return result
    
def set_scene_numbering(indexer_id, season, episode, sceneSeason=None, sceneEpisode=None):
    """
    Set scene numbering for a season/episode.
    To clear the scene numbering, leave both sceneSeason and sceneEpisode as None.
    
    """
    if indexer_id is None or season is None or episode is None:
        return
    
    _check_for_schema()
    myDB = db.DBConnection()
    
    # sanity
    #if sceneSeason == None: sceneSeason = season
    #if sceneEpisode == None: sceneEpisode = episode
    
    # delete any existing record first
    myDB.action('DELETE FROM scene_numbering where indexer_id = ? and season = ? and episode = ?', [indexer_id, season, episode])
    
    # now, if the new numbering is not the default, we save a new record
    if sceneSeason is not None and sceneEpisode is not None:
        myDB.action("INSERT INTO scene_numbering (indexer_id, season, episode, scene_season, scene_episode) VALUES (?,?,?,?,?)", [indexer_id, season, episode, sceneSeason, sceneEpisode])
            
            
def find_xem_numbering(indexer_id, season, episode):
    """
    Returns the scene numbering, as retrieved from xem.
    Refreshes/Loads as needed.
    
    @param indexer_id: int
    @param season: int
    @param episode: int
    @return: (int, int) a tuple of scene_season, scene_episode, or None if there is no special mapping.
    """
    if indexer_id is None or season is None or episode is None:
        return None
    
    _check_for_schema()
    if _xem_refresh_needed(indexer_id):
        _xem_refresh(indexer_id)
        
    cacheDB = db.DBConnection('cache.db')
    rows = cacheDB.select("SELECT scene_season, scene_episode FROM xem_numbering WHERE indexer_id = ? and season = ? and episode = ?", [indexer_id, season, episode])
    if rows:
        return (int(rows[0]["scene_season"]), int(rows[0]["scene_episode"]))
    else:
        return None
    
def get_indexer_numbering_for_xem(indexer_id, sceneSeason, sceneEpisode):
    """
    Reverse of find_xem_numbering: lookup a tvdb season and episode using scene numbering
    
    @param indexer_id: int
    @param sceneSeason: int
    @param sceneEpisode: int
    @return: (int, int) a tuple of (season, episode)   
    """
    if indexer_id is None or sceneSeason is None or sceneEpisode is None:
        return None
    
    _check_for_schema()
    if _xem_refresh_needed(indexer_id):
        _xem_refresh(indexer_id)
    cacheDB = db.DBConnection('cache.db')
    rows = cacheDB.select("SELECT season, episode FROM xem_numbering WHERE indexer_id = ? and scene_season = ? and scene_episode = ?", [indexer_id, sceneSeason, sceneEpisode])
    if rows:
        return (int(rows[0]["season"]), int(rows[0]["episode"]))
    else:
        return (sceneSeason, sceneEpisode)
    
def _xem_refresh_needed(indexer_id):
    """
    Is a refresh needed on a show?
    
    @param indexer_id: int
    @return: bool
    """
    if indexer_id is None:
        return False
    
    _check_for_schema()
    cacheDB = db.DBConnection('cache.db')
    rows = cacheDB.select("SELECT last_refreshed FROM xem_refresh WHERE indexer_id = ?", [indexer_id])
    if rows:
        return time.time() > (int(rows[0]['last_refreshed']) + MAX_XEM_AGE_SECS)
    else:
        return True
    
def _xem_refresh(indexer_id):
    """
    Refresh data from xem for a tv show
    
    @param indexer_id: int
    """
    if indexer_id is None:
        return
    
    try:
        logger.log(u'Looking up XEM scene mapping for show %s' % (indexer_id,), logger.DEBUG)
        #data = getURL('http://thexem.de/map/all?id=%s&origin=tvdb&destination=scene' % (indexer_id,))
        data = requests.get('http://thexem.de/map/all?id=%s&origin=tvdb&destination=scene' % (indexer_id,)).json()
        # http://thexem.de/map/all?id=1640  91&origin=tvdb&destination=scene
        if data is None or data == '':
            logger.log(u'No XEN data for show "%s", trying TVTumbler' % (indexer_id,), logger.MESSAGE)
            #data = getURL('http://show-api.tvtumbler.com/api/thexem/all?id=%s&origin=tvdb&destination=scene' % (indexer_id,))
            data = requests.get('http://show-api.tvtumbler.com/api/thexem/all?id=%s&origin=tvdb&destination=scene' % (indexer_id,)).json()
            if data is None or data == '':
                logger.log(u'TVTumbler also failed for show "%s".  giving up.' % (indexer_id,), logger.MESSAGE)
                return None
        result = data
        if result:
            _check_for_schema()
            cacheDB = db.DBConnection('cache.db')
            cacheDB.action("INSERT OR REPLACE INTO xem_refresh (indexer_id, last_refreshed) VALUES (?,?)", [indexer_id, time.time()])
            if 'success' in result['result']:
                cacheDB.action("DELETE FROM xem_numbering where indexer_id = ?", [indexer_id])
                for entry in result['data']:
                    if 'scene' in entry:
                        cacheDB.action("INSERT INTO xem_numbering (indexer_id, season, episode, scene_season, scene_episode) VALUES (?,?,?,?,?)",
                                    [indexer_id, entry['tvdb']['season'], entry['tvdb']['episode'], entry['scene']['season'], entry['scene']['episode'] ])
                    if 'scene_2' in entry: # for doubles
                        cacheDB.action("INSERT INTO xem_numbering (indexer_id, season, episode, scene_season, scene_episode) VALUES (?,?,?,?,?)",
                                    [indexer_id, entry['tvdb']['season'], entry['tvdb']['episode'], entry['scene_2']['season'], entry['scene_2']['episode'] ])

                #logger.log(u'Found XEM scene data for show %s' % (indexer_id), logger.MESSAGE)
            else:
                logger.log(u'Failed to get XEM scene data for show %s because "%s"' % (indexer_id, result['message']), logger.MESSAGE)
        else:
            logger.log(u"Empty lookup result - no XEM data for show %s" % (indexer_id,), logger.MESSAGE)
    except Exception, e:
        logger.log(u"Exception while refreshing XEM data for show " + str(indexer_id) + ": " + ex(e), logger.WARNING)
        logger.log(traceback.format_exc(), logger.DEBUG)
        return None
    
def get_xem_numbering_for_show(indexer_id):
    """
    Returns a dict of (season, episode) : (sceneSeason, sceneEpisode) mappings
    for an entire show.  Both the keys and values of the dict are tuples.
    Will be empty if there are no scene numbers set in xem
    """
    if indexer_id is None:
        return {}
    
    _check_for_schema()
    if _xem_refresh_needed(indexer_id):
        _xem_refresh(indexer_id)
    cacheDB = db.DBConnection('cache.db')
        
    rows = cacheDB.select('''SELECT season, episode, scene_season, scene_episode 
                        FROM xem_numbering WHERE indexer_id = ?
                        ORDER BY season, episode''', [indexer_id])
    result = {}
    for row in rows:
        result[(int(row['season']), int(row['episode']))] = (int(row['scene_season']), int(row['scene_episode']))
        
    return result

def get_xem_numbering_for_season(indexer_id, season):
    """
    Returns a dict of (season, episode) : (sceneSeason, sceneEpisode) mappings
    for an entire show.  Both the keys and values of the dict are tuples.
    Will be empty if there are no scene numbers set
    """
    if indexer_id is None or season is None:
        return {}

    _check_for_schema()
    if _xem_refresh_needed(indexer_id):
        _xem_refresh(indexer_id)

    cacheDB = db.DBConnection('cache.db')

    rows = cacheDB.select('''SELECT season, scene_season
                        FROM xem_numbering WHERE indexer_id = ? AND season = ?
                        ORDER BY season''', [indexer_id, season])
    result = {}
    if rows:
        for row in rows:
            result.setdefault(int(row['season']),[]).append(int(row['scene_season']))
    else:
        result.setdefault(int(season),[]).append(int(season))

    return result