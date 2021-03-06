import aiohttp
import asyncio
import io
import os
import queue
import shutil
import logging
import inspect
import re
from os import path
# for setting pipe buffer size
try:
    import fcntl
    import platform
except:
    pass
# for simple util functions
from .common import *
# for segments downloader
from .bilibili_api import VideoSegmentDownloader
# for bilibili data classes
from .bilibili_data import *
# for unblocked file io
from .buffered_writer import FileWriter
# for database update
from .db import VideoDatabase, VideoStatus

logger = logging.getLogger(__name__)


def save_to_file(file_name, content):
    '''Write a BytesIO into a file and close the BytesIO
    '''
    with open(file_name, 'wb') as f:
        try:
            content.seek(0)
            shutil.copyfileobj(content, f)
        except:
            logger.exception('save bytesIO to file failed')
        finally:
            content.close()


class DiscordPlayer:
    '''Base class for bilibili player
    '''
    _page_size = 4096
    _block_size = 32 * _page_size
    _pipe_buffer_size = 256 * _page_size

    def __init__(self, voice, loop, segments, after, *, video_info=None, path=None, **kwargs):
        self.voice = voice
        self.loop = loop if loop is not None else asyncio.get_event_loop()
        self.segments = segments
        self.after = after
        self.video_info = video_info
        self.path = path

        self.finish_event = asyncio.Event()
        self.task = None
        self.pin = None
        self.player = None

    def _set_pipe_buffer_size(self, fd, size):
        try:
            system = platform.system()
            if system == 'Linux':
                fcntl.F_SETPIPE_SZ = 1031
                fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, size)
        except IOError:
            logger.info('change pipe buffer size failed')

    def _after_callback(self):
        logger.info('player after callback called')
        try:
            self.loop.call_soon_threadsafe(self.finish_event.set)
            self._call_after()
        except:
            logger.exception('player called after failed')

    def _create_piped_player(self):
        pipeout, pipein = os.pipe()
        self._set_pipe_buffer_size(pipein, self._pipe_buffer_size)
        self._set_pipe_buffer_size(pipeout, self._pipe_buffer_size)
        self.player = self.voice.create_ffmpeg_player(
            os.fdopen(pipeout, 'rb'), pipe=True, after=self._after_callback)
        return os.fdopen(pipein, 'wb')

    async def run(self):
        logger.info('start running of discord player')
        try:
            self.task = self.loop.create_task(self._task())
            await self.task
        except CancelledError:
            logger.info('player task have been cancelled')
        except:
            logger.exception('player task running failed')

    async def _task(self):
        self.finish_event.clear()
        await self._do_run()

    def _call_after(self):
        if self.after is not None:
            try:
                arg_count = len(inspect.signature(self.after).parameters)
            except:
                arg_count = 0
            try:
                logger.info('call after with %d param' % arg_count)
                if arg_count == 0:
                    self.after()
                else:
                    self.after(self)
            except:
                logger.exception('player call after failed')

    def stop(self):
        logger.info('stop running of discord player')
        self.player.stop()
        if self.task is not None:
            logger.info('trying to cancel existing task')
            self.task.cancel()

    def is_done(self):
        return self.player.is_done()

    @property
    def title(self):
        if self.video_info is not None:
            return self.video_info.title
        return ''

    @property
    def duration(self):
        if self.video_info is not None:
            return self.video_info.duration
        return 0

    @property
    def uploader(self):
        if self.video_info is not None:
            return self.video_info.uploader
        return ''


class BiliLocalPlayer(DiscordPlayer):
    '''Local player for bilibili
    '''

    def __init__(self, voice, loop, segments, after, *, video_info=None, path=None, **kwargs):
        super().__init__(voice, loop, segments, after,
                         video_info=video_info, path=path, **kwargs)

    def _feedFile(self, segment):
        logger.info('local player feed file started for segment: %s' %
                    str(segment))
        file_name = path.join(self.path, segment.file_name)
        try:
            with open(file_name, 'rb') as fin:
                shutil.copyfileobj(fin, self.pin)
        except:
            logger.exception('feed file failed')

    async def _do_run(self):
        logger.info('start local player')
        for segment in self.segments:
            self.pin = self._create_piped_player()
            self.player.start()
            await self.loop.run_in_executor(None, self._feedFile, segment)
            self.pin.close()
            await self.finish_event.wait()


class BiliOnlinePlayer(DiscordPlayer):
    '''Online Player for bilibili
    '''
    _bili_address = 'https://www.bilibili.com'
    _user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/66.0.3359.117 Safari/537.36'

    def __init__(self, voice, loop, segments, ref_url, after, *, video_info=None, path=path, **kwargs):
        super().__init__(voice, loop, segments, after,
                         video_info=video_info, path=path, **kwargs)
        logger.info('created online player for %s' % ref_url)
        self.segments = segments
        self.url = ref_url
        self.session = aiohttp.ClientSession()

    async def _do_download(self):
        for segment in self.segments:
            logger.info('start online player for %s' % str(segment))
            f = None
            if self.path is not None:
                file_name = path.join(self.path, segment.file_name)
                f = FileWriter(file_name)
            try:
                self.pin = self._create_piped_player()
                downloader = VideoSegmentDownloader(
                    self.url, self.session, segment, self.loop)
                logger.info('online player download started')
                self.player.start()
                await downloader.download(self.pin, f)
                self.pin.close()
                await self.finish_event.wait()
            except CancelledError:
                return
            except Exception as e:
                logger.exception('online player failed')
            finally:
                if f is not None:
                    f.close()

        await self.loop.run_in_executor(None, self._write_segments, self.segments)

    def _write_segments(self, segments):
        if self.path is None:
            return

        db = VideoDatabase()
        aid = int(re.search(r'av(\d+)', url).group(1))
        seg_json = json.dumps(segments, default=obj_dict)
        db.update_segmentinfo(aid, seg_json)

    async def _do_run(self):
        logger.info('start online player')
        await self._do_download()

    def __del__(self):
        self.loop.call_soon(self.session.close)
