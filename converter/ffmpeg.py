#!/usr/bin/env python

import os.path
import os
import re
import signal
from urllib3.util import parse_url
from subprocess import Popen, PIPE
import logging
import locale
import json
import time

logger = logging.getLogger(__name__)

console_encoding = locale.getdefaultlocale()[1] or 'UTF-8'


class FFMpegError(Exception):
    pass


class SeekError(FFMpegError):
    pass


class DVDError(FFMpegError):
    pass


class FFMpegConvertError(Exception):
    def __init__(self, message, cmd, output, details=None, pid=0):
        """
        @param    message: Error message.
        @type     message: C{str}

        @param    cmd: Full command string used to spawn ffmpeg.
        @type     cmd: C{str}

        @param    output: Full stdout output from the ffmpeg command.
        @type     output: C{str}

        @param    details: Optional error details.
        @type     details: C{str}
        """
        super(FFMpegConvertError, self).__init__(message)

        self.cmd = cmd
        self.output = output
        self.details = details
        self.pid = pid

    def __repr__(self):
        error = self.details if self.details else self.message
        return ('<FFMpegConvertError error="%s", pid=%s, cmd="%s">' %
                (error, self.pid, self.cmd))

    def __str__(self):
        return self.__repr__()


class FFMpeg(object):
    """
    FFMPeg wrapper object, takes care of calling the ffmpeg binaries,
    passing options and parsing the output.

    >>> f = FFMpeg()
    """
    DEFAULT_JPEG_QUALITY = 4
    AUDIO_PEAK_MAX = -1  # dBTP
    AUDIO_LOUDNESS_TARGET = -16  # LUFS
    DVD_CONCAT_FILE = '/tmp/dvd_concat.txt'

    def __init__(self, ffmpeg_path=None, ffprobe_path=None, dvd2concat_path=None):
        """
        Initialize a new FFMpeg wrapper object. Optional parameters specify
        the paths to ffmpeg and ffprobe utilities.
        """

        self.current_process = None

        def which(name):
            path = os.environ.get('PATH', os.defpath)
            for d in path.split(':'):
                fpath = os.path.join(d, name)
                if os.path.exists(fpath) and os.access(fpath, os.X_OK):
                    return fpath
            return None

        if ffmpeg_path is None:
            ffmpeg_path = 'ffmpeg'

        if ffprobe_path is None:
            ffprobe_path = 'ffprobe'

        if dvd2concat_path is None:
            dvd2concat_path = 'dvd2concat'

        if '/' not in ffmpeg_path:
            ffmpeg_path = which(ffmpeg_path) or ffmpeg_path
        if '/' not in ffprobe_path:
            ffprobe_path = which(ffprobe_path) or ffprobe_path
        if '/' not in dvd2concat_path:
            dvd2concat_path = which(dvd2concat_path) or dvd2concat_path

        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.dvd2concat_path = dvd2concat_path

        if not os.path.exists(self.ffmpeg_path):
            raise FFMpegError("ffmpeg binary not found: " + self.ffmpeg_path)

        if not os.path.exists(self.ffprobe_path):
            raise FFMpegError("ffprobe binary not found: " + self.ffprobe_path)

    @staticmethod
    def _spawn(cmds, stdin=PIPE):
        logger.debug('Spawning ffmpeg with command: ' + ' '.join(cmds))
        return Popen(cmds, shell=False, stdin=stdin, stdout=PIPE, stderr=PIPE,
                     close_fds=True)

    def stop(self):
        if self.current_process:
            try:
                self.current_process.terminate()
            except (OSError, AttributeError):
                raise FFMpegError("Can't stop FFmpeg")

    def _check_vob_name(self, source):
        match = re.search('/VIDEO_TS/(VTS_\d\d_)\d(.VOB)$', source, re.IGNORECASE)
        if match is None:
            return source

        base_name = match.group(1)
        extension = match.group(2)
        path = os.path.dirname(source)
        items = os.listdir(path)
        vobs = [os.path.join(path, item) for item in items
                if re.match('{0}[1-9]{1}'.format(base_name, extension), item)]
        if len(vobs) == 1:
            return source
        vobs.sort()
        vobs = '|'.join(vobs)
        return 'concat:{0}'.format(vobs)

    def _dvd2concat(self, source):
        match = re.search('(.+)/VIDEO_TS/VTS_(\d\d)_\d.VOB$', source, re.IGNORECASE)
        if match is None:
            return source

        if not os.path.exists(self.dvd2concat_path):
            raise FFMpegError("dvd2concat script not found: " + self.dvd2concat_path)

        source = match.group(1)
        title = str(int(match.group(2)))

        p = self._spawn([self.dvd2concat_path, '-title', title, source])
        stdout_data, _ = p.communicate()
        stdout_data = stdout_data.decode(console_encoding, 'ignore')
        if not stdout_data.startswith(u'ffconcat version 1.0'):
            raise Exception("Invalid concat data from dvd2concat")

        with open(self.DVD_CONCAT_FILE, 'w') as concat_file:
            concat_file.write(stdout_data)

        return self.DVD_CONCAT_FILE

    def is_url(self, url):
        #: Accept objects that have string representations.
        try:
            url = unicode(url)
        except NameError:
            # We're on Python 3.
            url = str(url)
        except UnicodeDecodeError:
            pass

        # Support for unicode domain names and paths.
        scheme, auth, host, port, path, query, fragment = parse_url(url)

        if not scheme or not host:
            return False

        # Only want to apply IDNA to the hostname
        try:
            host = host.encode('idna').decode('utf-8')
        except UnicodeError:
            return False

        return True

    def probe(self, fname, posters_as_video=False, title=None):
        """
        Examine the media file and determine its format and media streams.
        Returns the MediaInfo object, or None if the specified file is
        not a valid media file.

        >>> info = FFMpeg().probe('test1.ogg')
        >>> info['container']
        'ogg'
        >>> info['duration']
        33.00
        >>> info['video']['codec']
        'theora'
        >>> info['video']['width']
        720
        >>> info['video']['height']
        400
        >>> info['audio']['codec']
        'vorbis'
        >>> info['audio']['channels']
        2
        :param posters_as_video: Take poster images (mainly for audio files) as
            A video stream, defaults to False
        """
        if not os.path.exists(fname) and not self.is_url(fname):
            return None

        p = self._spawn([self.ffprobe_path, '-v', 'quiet', '-print_format',
                        'json', '-show_format', '-show_streams', fname])
        stdout_data, _ = p.communicate()
        stdout_data = stdout_data.decode(console_encoding, 'ignore')
        info = json.loads(stdout_data)

        info['posters'] = []
        if 'streams' in info:
            for stream in info['streams']:
                try:
                    attached_pic = stream['disposition']['attached_pic']
                except KeyError:
                    attached_pic = None
                if stream['codec_type'] not in info:
                    info[stream['codec_type'] + 's'] = []
                    if (stream['codec_type'] != 'video'
                            or (posters_as_video or not attached_pic)):
                        info[stream['codec_type']] = stream
                info[stream['codec_type'] + 's'].append(stream)
                if attached_pic:
                    info['posters'].append(stream)

        def de_under(key, value, branch):
            branch[key.replace('_', '')] = value

        def lower(key, value, branch):
            branch[key] = value.lower()

        def container(key, value, branch):
            info['container'] = value.lower().split(',')

        def extension(key, value, branch):
            info['extension'] = os.path.splitext(value)[1][1:].lower()

        def codec(key, value, branch):
            if 'aac' in value:
                value = 'aac'
            branch['codec'] = value.lower()

        def bitrate_in_kbps(key, value, branch):
            branch['bitrate'] = round(value / 1000.0)

        def bitrate_in_mbps(key, value, branch):
            branch['bitrate'] = round(value / 1000.0 / 1000, 1)

        def fps(key, value, branch):
            branch['fps'] = round(value, 2)

        def level(key, value, branch):
            branch[key] = round(value / 10.0, 1)

        FIELD_MAPPING = {
            'format': {
                'format_name': container,
                'filename': extension,
                'bit_rate': bitrate_in_mbps,
            },
            'audios': {
                'codec_name': codec,
                'sample_rate': de_under,
                'bit_rate': bitrate_in_kbps,
            },
            'videos': {
                'codec_name': codec,
                'avg_frame_rate': fps,
                'bit_rate': bitrate_in_mbps,
                'profile': lower,
                'level': level,
            }
        }

        def clean_info(branch, parent=None):
            if parent is None:
                mapping = FIELD_MAPPING
            else:
                mapping = FIELD_MAPPING.get(parent, {})

            for key, value in branch.items():
                if key in ('streams', 'audio', 'video'):
                    continue

                if isinstance(value, dict):
                    clean_info(value, key)
                    continue

                if isinstance(value, list):
                    for item in value:
                        clean_info(item, key)
                    continue

                if isinstance(value, basestring):
                    try:
                        if '/' in value:
                            n, d = value.split('/', 1)
                            value = float(n) / float(d)
                        elif '.' in value:
                            value = float(value)
                        else:
                            value = int(value)
                    except ZeroDivisionError:
                        value = 0
                    except ValueError:
                        value = value.strip()
                    branch[key] = value

                if key in mapping:
                    mapping[key](key, value, branch)

        clean_info(info)

        # For .VOB file get duration with lsdvd.
        # fname = self._check_vob_name(fname)
        ext = os.path.splitext(fname)[1].upper()
        if (ext == '.ISO'
            or (ext == '.VOB'
                and ('VIDEO_TS/VTS_' in fname or '/tccat_L' in fname))):

            def update_duration(data, duration):
                if isinstance(data, dict):
                    if 'duration' in data:
                        # Keep ffprobe duration if difference with lsdvd
                        # duration is less then 1%.
                        probe_duration = data['duration']
                        gap = 2
                        if isinstance(probe_duration, (float, int)) and probe_duration > 0:
                            if probe_duration > duration:
                                gap = probe_duration / duration
                            else:
                                gap = duration / probe_duration
                        if gap > 1.01:
                            data['duration'] = duration
                        data['duration_lsdvd'] = duration
                        data['duration_probe'] = probe_duration

                    for value in data.values():
                        update_duration(value, duration)
                elif isinstance(data, (tuple, list)):
                    for item in data:
                        update_duration(item, duration)

            if '/tccat_L' in fname:
                try:
                    duration = float(fname.split('/tccat_L', 1)[1].split('_', 1)[0])
                except Exception:
                    pass
                else:
                    update_duration(info, duration)
                    if not 'duration' in info['format']:
                        info['format']['duration'] = duration
            else:
                if ext == '.VOB':
                    # part = fname.rsplit('|', 1)[-1]
                    fname, end = fname.split('VIDEO_TS/VTS_', 1)
                    if title is None:
                        title = int(end.split('_', 1)[0])
                p = self._spawn(['lsdvd', '-q', '-Oy', '-t', str(title), fname])
                stdout_data, _ = p.communicate()
                stdout_data = stdout_data.decode(console_encoding, 'ignore')
                try:
                    exec stdout_data in locals()
                    duration = float(lsdvd['track'][0]['length'])
                except Exception:
                    pass
                else:
                    update_duration(info, duration)
                    if not 'duration' in info['format']:
                        info['format']['duration'] = duration

        return info

    def convert(self, infile, outfile, opts, timeout=10, nice=None, get_output=False, title=None):
        """
        Convert the source media (infile) according to specified options
        (a list of ffmpeg switches as strings) and save it to outfile.

        Convert returns a generator that needs to be iterated to drive the
        conversion process. The generator will periodically yield timecode
        of currently processed part of the file (ie. at which second in the
        content is the conversion process currently).

        The optional timeout argument specifies how long should the operation
        be blocked in case ffmpeg gets stuck and doesn't report back. See
        the documentation in Converter.convert() for more details about this
        option.

        >>> conv = FFMpeg().convert('test.ogg', '/tmp/output.mp3',
        ...    ['-acodec libmp3lame', '-vn'])
        >>> for timecode in conv:
        ...    pass # can be used to inform the user about conversion progress

        """
        if not os.path.exists(infile) and not self.is_url(infile):
            raise FFMpegError("Input file doesn't exist: " + infile)

        # infile = self._dvd2concat(infile)

        cmds = [self.ffmpeg_path, '-hide_banner']
        if infile == self.DVD_CONCAT_FILE:
            cmds.extend(['-f', 'concat', '-safe', '0'])
        # Add duration and position flag before input when we can.
        if '-t' in opts:
            idx = opts.index('-t')
            cmds.append(opts.pop(idx))
            cmds.append(opts.pop(idx))
        if '-ss' in opts and ('-t' in opts or '-to' not in opts):
            idx = opts.index('-ss')
            cmds.append(opts.pop(idx))
            cmds.append(opts.pop(idx))

        ext = os.path.splitext(infile)[1].upper()
        if get_output and (ext == '.ISO'
                           or (ext == '.VOB' and 'VIDEO_TS/VTS_' in infile)):
            cmds.extend(['-i', 'pipe:'])
        else:
            cmds.extend(['-i', infile])
        cmds.extend(opts)
        cmds.extend(['-y', outfile])

        return self._run_ffmpeg(infile, cmds, timeout=timeout, nice=nice, get_output=get_output, title=title)

    def _run_ffmpeg(self, infile, cmds, timeout=10, nice=None, get_output=False, title=None):
        if nice is not None:
            if 0 < nice < 20:
                cmds = ['nice', '-n', str(nice)] + cmds
            else:
                raise FFMpegError("Invalid nice value: {0}".format(nice))

        if 'pipe:' in cmds:
            if infile.upper().endswith('.VOB'):
                infile = os.path.dirname(infile)
            nice = cmds[0:3] if cmds[0] == 'nice' else []
            piped_cmds = nice + ['tccat', '-i', infile, '-T', str(title) + ',-1']
            preprocess = self._spawn(piped_cmds)
        else:
            preprocess = None

        try:
            if preprocess:
                self.current_process = self._spawn(cmds, preprocess.stdout)
                preprocess.stdout.close()
            else:
                self.current_process = self._spawn(cmds)
            p = self.current_process
        except OSError:
            raise FFMpegError('Error while calling ffmpeg binary')

        if timeout:
            def on_sigvtalrm(*_):
                try:
                    signal.signal(signal.SIGVTALRM, signal.SIG_DFL)
                except ValueError:
                    pass
                if p.poll() is None:
                    p.kill()
                raise Exception('timed out while waiting for ffmpeg called with this command: {0}'.format(' '.join(cmds)))

            try:
                signal.signal(signal.SIGVTALRM, on_sigvtalrm)
            except ValueError:
                pass

        yielded = False
        buf = []
        total_output = []
        pat = re.compile(r'time=([0-9.:]+)')

        while True:
            if timeout:
                signal.setitimer(signal.ITIMER_VIRTUAL, timeout)

            ret = p.stderr.read(10)

            if timeout:
                signal.setitimer(signal.ITIMER_VIRTUAL, 0)

            if not ret:
                break

            total_output.append(ret)
            buf.append(ret)
            if '\r' in ret:
                buf = ''.join(buf)
                buf = buf.decode(console_encoding, 'ignore')
                line, buf = buf.split('\r', 1)
                buf = [buf]
                tmp = pat.search(line)
                if tmp:
                    timecode = timecode_to_seconds(tmp.group(1))
                    yielded = True
                    yield timecode

        total_output = ''.join(total_output)
        total_output = total_output.decode(console_encoding, 'ignore')
        if not yielded:
            # There may have been a single time, check it
            tmp = pat.search(total_output)
            if tmp:
                timecode = timecode_to_seconds(tmp.group(1))
                yielded = True
                yield timecode

        if timeout:
            try:
                signal.signal(signal.SIGVTALRM, signal.SIG_DFL)
            except ValueError:
                pass

        p.communicate()  # wait for process to exit
        if preprocess:
            preprocess.terminate()
            preprocess.wait()

        if not total_output:
            raise FFMpegError('Error while calling ffmpeg binary')

        cmd = ' '.join(cmds)
        if '\n' in total_output:
            line = total_output.split('\n')[-2]

            if line.startswith('Received signal'):
                # Received signal 15: terminating.
                raise FFMpegConvertError(line.split(':')[0], cmd, total_output, pid=p.pid)
            if line.startswith(infile):
                err = line[len(infile) + 2:]
                raise FFMpegConvertError('Encoding error', cmd, total_output,
                                         err, pid=p.pid)
            if line.startswith('Error while '):
                raise FFMpegConvertError('Encoding error', cmd, total_output,
                                         line, pid=p.pid)
            if not yielded:
                raise FFMpegConvertError('Unknown ffmpeg error', cmd,
                                         total_output, line, pid=p.pid)
            if get_output:
                yield total_output
        if p.returncode != 0:
            raise FFMpegConvertError('Exited with code %d' % p.returncode, cmd,
                                     total_output, pid=p.pid)

    def analyze(self, infile, audio_level=True, interlacing=True, crop=False, start=None, duration=None, end=None, timeout=10, nice=None, title=None):
        """
        Analyze the video frames to find if the video need to be deinterlaced
        and/or crop to remove black strips.
        Analyze the audio to find if the audio need to be normalize
        and by how much. All analyses are together so FFMpeg can do them
        in the same pass.
        """
        if not audio_level and not interlacing and not crop:
            raise FFMpegError('Nothing selected to analyze (audio level, '
                              'interlacing or crop).')

        opts = ['-f', 'null']
        if audio_level:
            opts.extend(['-af', 'ebur128=peak=true:framelog=verbose'])
        else:
            opts.append('-an')

        video_filters = []
        if interlacing:
            video_filters.append('idet')

        if crop:
            video_filters.append('cropdetect=0.12:2:1')

        if video_filters:
            video_filters = ','.join(video_filters)
            opts.extend(['-vf', video_filters])
        else:
            opts.append('-vn')

        if start:
            start = parse_time(start)
            opts.extend(['-ss', start])

        if duration:
            duration = parse_time(duration)
            opts.extend(['-t', duration])
        elif end:
            end = parse_time(end)
            # Convert end to duration if start is defined, so the convert
            # function can put them before the input. If not, the analyze will
            # be done on the entire file and that will be wrong.
            if start:
                duration = timecode_to_seconds(end) - timecode_to_seconds(start)
                opts.extend(['-t', str(duration)])
            else:
                opts.extend(['-to', end])

        for data in self.convert(infile, '/dev/null',
                                 opts, timeout, nice=nice, get_output=True):
            if isinstance(data, float):
                yield data
            else:
                interlace = None
                adjustement = None
                crop_size = None

                if audio_level:
                    match = re.search('Integrated loudness:\s+I:\s+(-?\d+\.\d)\s+LUFS(?s).+True peak:\s+Peak:\s+(-?\d+\.\d)\s+dBFS', data, re.UNICODE)
                    if match is None:
                        adjustement = 'noise'
                    else:
                        # Adjust audio volume so loudness will be close as possible
                        # as the target but max peak will not be above AUDIO_PEAK_MAX.
                        loudness = float(match.group(1))
                        peak = float(match.group(2))
                        # Check if audio is only noise.
                        # Values are good only for 16 bits audio.
                        if loudness < -56 and peak < -40:
                            adjustement = 'noise'
                        else:
                            loudness_adj = self.AUDIO_LOUDNESS_TARGET - loudness
                            if loudness_adj > 0:
                                peak_adj = self.AUDIO_PEAK_MAX - peak
                                if peak_adj < 0:
                                    peak_adj = 0
                                adjustement = min(peak_adj, loudness_adj)
                            else:
                                adjustement = loudness_adj
                            # Don't adjust if adjustment is too small
                            if -1 < adjustement < 1:
                                adjustement = 0

                if interlacing:
                    match = re.search('Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)', data, re.UNICODE)
                    if match is None:
                        raise FFMpegConvertError(
                            'No interlaced data.',
                            opts,
                            data
                        )
                    tff = int(match.group(1))
                    bff = int(match.group(2))
                    progressive = int(match.group(3))
                    undetermined = int(match.group(4))
                    interlaced = tff + bff
                    total = interlaced + progressive + undetermined
                    # If more then 10% of frames are detected as interlaced,
                    # assume video is at least partly interlaced frames.
                    if interlaced > total / 10:
                        interlace = True
                    else:
                        interlace = False

                if crop:
                    info = self.probe(infile, title=title)
                    video = info['video']
                    size = (video['width'], video['height'])
                    fps = video.get('fps', 29.97)
                    crop_size = parse_crop(data, size, fps)

                yield adjustement, interlace, crop_size

    def thumbnails_by_interval(self, source, output_pattern, interval=1,
                               max_width=None, max_height=None, autorotate=False,
                               sizing_policy=None, skip=False, title=None):
        """
        Create one or more thumbnails of video by a specified interval.
        """
        info = self.probe(source, title=title)
        if 'video' not in info:
            raise ValueError("Video stream not found.")

        video = info['video']

        src_width = video['video_width']
        src_height = video['video_height']
        w, h, filters = self._aspect_corrections(src_width, src_height,
                                                 max_width, max_height,
                                                 sizing_policy)
        w = self._div_by_2(w)
        h = self._div_by_2(h)

        if autorotate and 'rotate' in video['tags']:
            rotate_filter = {
                90: "transpose=1",
                180: "transpose=2,transpose=2",
                270: "transpose=2"
            }
            raw_rotate = video['tags'].get('rotate')
            if raw_rotate and int(raw_rotate) in rotate_filter.keys():
                src_rotate = int(raw_rotate)
                # apply filter
                filters = '{0}{2}{1}'.format(filters or '',
                                             rotate_filter.get(src_rotate) or '',
                                             ',' if filters and rotate_filter.get(src_rotate) else '')
                # swap height and width if vertical rotation
                if src_rotate in [90, 270]:
                    old_w = w
                    old_h = h
                    w = old_h
                    h = old_w

        if not os.path.exists(source) and not self.is_url(source):
            raise IOError('No such file: ' + source)

        cmds = [self.ffmpeg_path, ]

        if skip:
            if info.format.duration > 10:
                cmds.extend(['-ss', '10'])
            else:
                cmds.extend(['-ss', str(int(info.format.duration / 2))])

        cmds.extend(['-i', source, '-y', '-an'])
        cmds.extend(['-f', 'image2'])
        cmds.extend(['-s', "{0}x{1}".format(w, h)])
        cmds.extend(['-q:v', str(FFMpeg.DEFAULT_JPEG_QUALITY)])

        if filters:
            cmds.extend(['-vf', 'fps=fps=1/{0},{1}'.format(interval, filters)])
        else:
            cmds.extend(['-vf', 'fps=fps=1/{0}'.format(interval)])

        cmds.extend([output_pattern.format(count="%05d")])

        p = self._spawn(cmds)
        _, stderr_data = p.communicate()
        if stderr_data == '':
            raise FFMpegError('Error while calling ffmpeg binary')
        stderr_data.decode(console_encoding, "ignore")

    def thumbnail(self, fname, time, outfile, size=None, quality=DEFAULT_JPEG_QUALITY, crop=None, deinterlace=None):
        """
        Create a thumbnal of media file, and store it to outfile
        @param time: time point in seconds (float or int) or in HH:MM:SS format.
        @param size: Size, if specified, is WxH of the desired thumbnail.
            If not specified, the video resolution is used.
        @param quality: quality of jpeg file in range 2(best)-31(worst)
            recommended range: 2-6
        @param crop: crop size for all images specified like in FFMpeg.
        @param deinterlace: True to apply deinterlacing on thumbnail if needed.

        >>> FFMpeg().thumbnail('test1.ogg', 5, '/tmp/shot.png', '320x240')
        """
        return self.thumbnails(fname, [(time, outfile, size, quality)], crop=None, deinterlace=None)

    def thumbnail_fast(self, fname, time, outfile, size=None, quality=DEFAULT_JPEG_QUALITY, crop=None, deinterlace=None):
        """
        Create a thumbnail of media file, and store it to outfile
        @param time: time point in seconds (float or int) or in HH:MM:SS format.
        @param size: Size, if specified, is WxH of the desired thumbnail.
            If not specified, the video resolution is used.
        @param quality: quality of jpeg file in range 2(best)-31(worst)
            recommended range: 2-6
        @param crop: crop size for all images specified like in FFMpeg.
        @param deinterlace: True to apply deinterlacing on thumbnail if needed.

        >>> FFMpeg().thumbnail('test1.ogg', 5, '/tmp/shot.png', '320x240')
        """
        if not os.path.exists(fname) and not self.is_url(fname):
            raise IOError('No such file: ' + fname)

        # fname = self._dvd2concat(fname)

        if fname == self.DVD_CONCAT_FILE:
            raise DVDError('Input is a DVD, need to use slow thumbnail extraction method.')

        cmds = [self.ffmpeg_path, '-ss', parse_time(time), '-i', fname, '-y',
                '-an', '-f', 'image2', '-vframes', '1']

        if crop or deinterlace:
            cmds.append('-vf')
            filters = []
            if deinterlace:
                filters.append('idet,yadif=0:deint=interlaced')
            if crop:
                filters.append('crop={0}'.format(crop))
            cmds.append(','.join(filters))
        if size:
            cmds.extend(['-s', str(size)])

        cmds.extend([
            '-q:v', str(FFMpeg.DEFAULT_JPEG_QUALITY),
            outfile
        ])

        p = self._spawn(cmds)
        _, stderr_data = p.communicate()
        if stderr_data == '':
            raise FFMpegError('Error while calling ffmpeg binary')
        stderr_data = stderr_data.decode(console_encoding, "ignore")
        if u'Output file is empty, nothing was encoded (check -ss / -t / -frames parameters if used)' in stderr_data:
            raise SeekError(stderr_data)
        if not os.path.exists(outfile):
            raise FFMpegError('Error creating thumbnail: %s' % stderr_data)

    def _div_by_2(self, d):
        return d+1 if d % 2 else d

    def _aspect_corrections(self, sw, sh, max_width, max_height, sizing_policy):
        if not sw or not sh:
            return sw, sh, None

        # Original aspect ratio
        aspect = (1.0 * sw) / (1.0 * sh)

        # If we have only one dimension, we can easily calculate
        # the other to match the source aspect ratio
        if not max_width and not max_height:
            return max_width, max_height, None
        elif max_width and not max_height:
            max_height = int((1.0 * max_width) / aspect)
        elif max_height and not max_width:
            max_width = int(aspect * max_height)

        if sizing_policy not in ['Fit', 'Fill', 'Stretch', 'Keep', 'ShrinkToFit', 'ShrinkToFill']:
            print "invalid option {0}".format(sizing_policy)
            return sw, sh, None

        """
        Fit: FFMPEG scales the output video so it matches the value
        that you specified in either Max Width or Max Height without exceeding the other value."
        """
        if sizing_policy == 'Fit':
            if float(sh / sw) == float(max_height):
                return max_width, max_height, None
            elif float(sh / sw) < float(max_height):  # scaling height
                factor = float(float(max_height) / float(sh))
                return int(max_width * factor), max_height, None
            else:
                factor = float(float(max_width) / float(sw))
                return max_width, int(sh * factor), None

        """
        Fill: FFMPEG scales the output video so it matches the value that you specified
        in either Max Width or Max Height and matches or exceeds the other value. Elastic Transcoder
        centers the output video and then crops it in the dimension (if any) that exceeds the maximum value.
        """
        if sizing_policy == 'Fill':
            if float(sh / sw) == float(max_height):
                return max_width, max_height, None
            elif float(sh / sw) < float(max_height):  # scaling width
                factor = float(float(max_width) / float(sw))
                h0 = int(sh * factor)
                dh = (h0 - max_height) / 2
                return max_width, h0, 'crop={0}:{1}:{2}:0'.format(max_width, max_height, dh)
            else:
                factor = float(float(max_height) / float(sh))
                w0 = int(sw * factor)
                dw = (w0 - max_width) / 2
                return w0, max_height, 'crop={0}:{1}:{2}:0'.format(max_width, max_height, dw)

        """
        Stretch: FFMPEG stretches the output video to match the values that you specified for Max
        Width and Max Height. If the relative proportions of the input video and the output video are different,
        the output video will be distorted.
        """
        if sizing_policy == 'Stretch':
            return max_width, max_height, None

        """
        Keep: FFMPEG does not scale the output video. If either dimension of the input video exceeds
        the values that you specified for Max Width and Max Height, Elastic Transcoder crops the output video.
        """
        if sizing_policy == 'Keep':
            return sw, sh, None

        """
        ShrinkToFit: FFMPEG scales the output video down so that its dimensions match the values that
        you specified for at least one of Max Width and Max Height without exceeding either value. If you specify
        this option, Elastic Transcoder does not scale the video up.
        """
        if sizing_policy == 'ShrinkToFit':
            if sh > max_height or sw > max_width:
                if float(sh / sw) == float(max_height):
                    return max_width, max_height, None
                elif float(sh / sw) < float(max_height):  # target is taller
                    factor = float(float(max_height) / float(sh))
                    return int(sw * factor), max_height, None
                else:
                    factor = float(float(max_width) / float(sw))
                    return max_width, int(sh * factor), None
            else:
                return sw, sh, None

        """
        ShrinkToFill: FFMPEG scales the output video down so that its dimensions match the values that
        you specified for at least one of Max Width and Max Height without dropping below either value. If you specify
        this option, Elastic Transcoder does not scale the video up.
        """
        if sizing_policy == 'ShrinkToFill':
            if sh < max_height or sw < max_width:
                if float(sh / sw) == float(max_height):
                    return max_width, max_height, None
                elif float(sh / sw) < float(max_height):  # scaling width
                    factor = float(float(max_width) / float(sw))
                    h0 = int(sh * factor)
                    dh = (h0 - max_height) / 2
                    return max_width, h0, 'crop=%d:%d:%d:0' % (max_width, max_height, dh)
                else:
                    factor = float(float(max_height) / float(sh))
                    w0 = int(sw * factor)
                    dw = (w0 - max_width) / 2
                    return w0, max_height, 'crop={0}:{1}:{2}:0'.format(max_width, max_height, dw)
            else:
                return int(sw*factor), max_height, None

        assert False, sizing_policy

    def thumbnails(self, fname, option_list, crop=None, deinterlace=None, no_slow=False):
        """
        Create one or more thumbnails of video.
        This method is pretty fast as it seek directly to the frame to extract.
        If there's a problem to seek, this method will fallback to
        thumbnails_slow.
        @param option_list: a list of tuples like:
            (time, outfile, size=None, quality=DEFAULT_JPEG_QUALITY)
            see documentation of `converter.FFMpeg.thumbnail()` for details.

        >>> FFMpeg().thumbnails('test1.ogg', [(5, '/tmp/shot.png', '320x240'),
        >>>                                   (10, '/tmp/shot2.png', None, 5)])
        """
        if not os.path.exists(fname) and not self.is_url(fname):
            raise IOError('No such file: ' + fname)

        errors = {}

        # Sort by timecode so there's more chance that some images can be
        # extracted without using the slow version for problematic video files.
        option_list.sort(key=time_sort)

        for idx, options in enumerate(option_list):
            yield '{0}/{1}'.format(idx + 1, len(option_list))
            time = options[0]
            outfile = options[1]
            size = options[2] if len(options) > 2 else None
            quality = options[3] if len(options) > 3 else FFMpeg.DEFAULT_JPEG_QUALITY

            try:
                self.thumbnail_fast(fname, time, outfile, size, quality, crop, deinterlace)
            except (DVDError, SeekError), err:
                if no_slow:
                    errors[outfile] = err
                else:
                    # Do all remaining thumbnails with the slow version.
                    for timecode in self.thumbnails_slow(fname, option_list[idx:], crop=crop, deinterlace=deinterlace, errors=errors):
                        yield timecode
                    raise StopIteration()

            except Exception, err:
                errors[outfile] = err

        if errors:
            messages = u'; '.join(
                u'{0} gives error: {1}'.format(outfile, error)
                for outfile, error in errors.iteritems()
            )
            raise FFMpegError(messages)

    def thumbnails_slow(self, fname, option_list, crop=None, deinterlace=None, errors=None, nice=None):
        """
        Create one or more thumbnails of video.
        @param option_list: a list of tuples like:
            (time, outfile, size=None, quality=DEFAULT_JPEG_QUALITY)
            see documentation of `converter.FFMpeg.thumbnail()` for details.

        >>> FFMpeg().thumbnails('test1.ogg', [(5, '/tmp/shot.png', '320x240'),
        >>>                                   (10, '/tmp/shot2.png', None, 5)])
        """
        if not os.path.exists(fname) and not self.is_url(fname):
            raise IOError('No such file: ' + fname)

        if errors is None:
            errors = {}

        fname = self._dvd2concat(fname)

        if fname == self.DVD_CONCAT_FILE:
            cmds = [self.ffmpeg_path, '-f', 'concat', '-safe', '0']
        else:
            cmds = [self.ffmpeg_path]

        cmds.extend(['-i', fname, '-y', '-an'])

        option_list.sort(key=time_sort)

        for thumb in option_list:
            if crop or deinterlace:
                cmds.append('-vf')
                filters = []
                if deinterlace:
                    filters.append('idet,yadif=0:deint=interlaced')
                if crop:
                    filters.append('crop={0}'.format(crop))
                cmds.append(','.join(filters))
            if len(thumb) > 2 and thumb[2]:
                cmds.extend(['-s', str(thumb[2])])

            cmds.extend([
                '-f', 'image2', '-vframes', '1',
                '-ss', parse_time(thumb[0]), thumb[1],
                '-q:v', str(FFMpeg.DEFAULT_JPEG_QUALITY if len(thumb) < 4 else str(thumb[3])),
            ])

        # Get latest time in seconds.
        latest_time = timecode_to_seconds(option_list[-1][0])
        start_time = time.time()

        for timecode in self._run_ffmpeg(fname, cmds, nice=15):
            yield int(round((time.time() - start_time) / latest_time * 100))

        for options in option_list:
            if not os.path.exists(options[1]):
                errors[options[1]] = u'File was not created.'

        if errors:
            messages = u'; '.join(
                u'{0} gives error: {1}'.format(outfile, error)
                for outfile, error in errors.iteritems()
            )
            raise FFMpegError(messages)


def timecode_to_seconds(timecode):
    """
    Convert a valid timecode representation (a str) in seconds (as float).
    """
    try:
        return float(timecode)
    except ValueError:
        hours, minutes, seconds = timecode.split(':', 2)
        return (((int(hours) * 60) + int(minutes)) * 60) + float(seconds)


def seconds_to_timecode(seconds):
    """
    Convert seconds in int or float to timecode representation.
    """
    milliseconds = int(round((seconds % 1) * 1000))
    seconds = int(seconds)
    minutes = seconds / 60
    seconds = seconds % 60
    hours = minutes / 60
    minutes = minutes % 60
    return '{0:02d}:{1:02d}:{2:02d}.{3:03d}'.format(
        hours, minutes, seconds, milliseconds
    )


def parse_time(time):
    try:
        return str(float(time))
    except ValueError:
        match = re.search('([0-2]?\d):([0-5]?\d):([0-5]?\d)(?:\.(\d{3}))?$', time)
        if match:
            return '{0:02d}:{1:02d}:{2:02d}.{3:03d}'.format(*(int(i) if i else 0 for i in match.groups()))

    raise ValueError("Invalid 'time'. Should be in seconds or HH:MM:SS.xxx format.")


def time_sort(options):
    return timecode_to_seconds(options[0])


def parse_crop(data, size, fps):
    width, height = size
    # Maximum width and height of a black border.
    x_limit = width / 4
    y_limit = height / 4
    # Get all the only positive crop values.
    matches = re.findall('crop=(\d{1,4}):(\d{1,4}):(\d{1,3}):(\d{1,3})', data)
    if not matches:
        # Check for negative crop values.
        match = re.search('crop=-?\d{1,4}:-?\d{1,4}:-?\d{1,4}:-?\d{1,4}', data)
        if match:
            # Don't crop at all because negative values means a lot of black.
            return '{0}:{1}:0:0'.format(width, height)
        else:
            raise FFMpegConvertError('No crop data.', '', data)

    # For each crop values, get or calculate the left, right, top and bottom
    # values.
    values = (
        (
            int(x), width - int(x) - int(crop_width),
            int(y), height - int(y) - int(crop_height)
        )
        for crop_width, crop_height, x, y in matches
    )
    # Regroup values by side.
    values = zip(*values)

    # Count the number of occurences of each width/height.
    def counter(collection, limit):
        counted = {}
        for item in set(collection):
            if item > limit:
                continue
            counted[item] = collection.count(item)
        return counted

    results = {
        'left': counter(values[0], x_limit),
        'right': counter(values[1], x_limit),
        'top': counter(values[2], y_limit),
        'bottom': counter(values[3], y_limit),
    }

    # For each side find the larger gap between the number of frames of each
    # dimension and keep the dimension before the gap.
    for pos, result in results.iteritems():
        if result:
            dims = result.keys()
            dims.sort()

            length = width if pos in ('left', 'right') else height
            threshold = int(round(length * 0.03))
            gap_coord = 0

            largest_gap = 0
            dim = dims[0]
            for idx in range(len(dims) - 1):
                # If we already found a gap, stop looking for another one too
                # far away.
                if gap_coord and dims[idx] - gap_coord > threshold:
                    break
                gap = result[dims[idx]] - result[dims[idx + 1]]
                if gap >= largest_gap:
                    largest_gap = gap
                    dim = dims[idx]
                    gap_coord = dim
            else:
                # Check last value.
                if result[dims[-1]] >= largest_gap:
                    dim = dims[-1]
            # Add a bit of padding to crop blurry transition.
            if dim:
                dim += 4  # add some padding
        else:
            dim = 0

        results[pos] = dim

    x_sub = float(results['left'] + results['right'])
    y_sub = float(results['top'] + results['bottom'])

    if width <= 720 and height <= 576 and x_sub / width < 0.06 and y_sub / height < 0.05:
        # Here we are dealing with NTSC/PAL artifacts, not with pillars, etc.
        # Recalculate the smaller crop to keep the ratio intact.
        if x_sub / width > y_sub / height:
            final_y_sub = int(round(x_sub * height / width))
            delta_y_sub = final_y_sub - y_sub
            delta_top_sub = int(round(delta_y_sub / 2))
            results['top'] += delta_top_sub
            results['bottom'] += int(delta_y_sub) - delta_top_sub
        else:
            final_x_sub = int(round(y_sub * width / height))
            delta_x_sub = final_x_sub - x_sub
            delta_left_sub = int(round(delta_x_sub / 2))
            results['left'] += delta_left_sub
            results['right'] += int(delta_x_sub) - delta_left_sub

        x_sub = results['left'] + results['right']
        y_sub = results['top'] + results['bottom']
        crop_width = int(width - x_sub)
        crop_height = int(height - y_sub)

    else:
        # Round to multiple of 2 for pillars because there will be no scaling.
        base_width = int(width - x_sub)
        base_height = int(height - y_sub)
        crop_width = base_width / 2 * 2
        crop_height = base_height / 2 * 2
        if x_sub:
            results['left'] = int(round(results['left'] * (width - crop_width) / x_sub))
        if y_sub:
            results['top'] = int(round(results['top'] * (height - crop_height) / y_sub))

    return '{0}:{1}:{2}:{3}'.format(crop_width, crop_height, results['left'], results['top'])
