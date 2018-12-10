#!/usr/bin/env python


class BaseCodec(object):
    """
    Base audio/video codec class.
    """

    encoder_options = {}
    codec_name = None
    ffmpeg_codec_name = None

    def parse_options(self, opt):
        if 'codec' not in opt or opt['codec'] != self.codec_name:
            raise ValueError('invalid codec name')
        return None

    def _codec_specific_parse_options(self, safe):
        return safe

    def _codec_specific_produce_ffmpeg_list(self, safe):
        return []

    def safe_options(self, opts):
        safe = {}

        # Only copy options that are expected and of correct type
        # (and do typecasting on them)
        for k, v in opts.items():
            if k in self.encoder_options:
                typ = self.encoder_options[k]
                try:
                    safe[k] = typ(v)
                except:
                    pass

        return safe


class AudioCodec(BaseCodec):
    """
    Base audio codec class handles general audio options. Possible
    parameters are:
      * codec (string) - audio codec name
      * channels (integer) - number of audio channels
      * bitrate (integer) - stream bitrate
      * samplerate (integer) - sample rate (frequency)

    Supported audio codecs are: null (no audio), copy (copy from
    original), vorbis, aac, mp3, mp2
    """

    encoder_options = {
        'codec': str,
        'channels': int,
        'bitrate': int,
        'samplerate': int,
        'filters': str
    }

    def _extend_af(self, optlist, value):

        if not value:
            return optlist

        if optlist.count('-af'):
            current_af = optlist[optlist.index('-af') + 1]
            new_af = "{0},{1}".format(current_af, value)  # append filters to current
            optlist[optlist.index('-af') + 1] = new_af
        else:
            optlist.extend(['-af', value])
        return optlist

    def parse_options(self, opt):
        super(AudioCodec, self).parse_options(opt)

        safe = self.safe_options(opt)

        if 'channels' in safe:
            c = safe['channels']
            if c < 1 or c > 12:
                del safe['channels']

        if 'bitrate' in safe:
            br = safe['bitrate']
            if br < 8 or br > 512:
                del safe['bitrate']

        if 'samplerate' in safe:
            f = safe['samplerate']
            if f < 1000 or f > 50000:
                del safe['samplerate']

        safe = self._codec_specific_parse_options(safe)

        optlist = ['-acodec', self.ffmpeg_codec_name]
        if 'channels' in safe:
            optlist.extend(['-ac', str(safe['channels'])])
        if 'bitrate' in safe:
            optlist.extend(['-ab', str(safe['bitrate']) + 'k'])
        if 'samplerate' in safe:
            optlist.extend(['-ar', str(safe['samplerate'])])
        if 'volume' in safe:
            optlist.extend(['-af', 'volume={0:.1f}dB'.format(safe['volume'])])
        if 'filters' in safe:
            optlist = self._extend_af(optlist, safe['filters'])

        optlist.extend(self._codec_specific_produce_ffmpeg_list(safe))
        return optlist


class SubtitleCodec(BaseCodec):
    """
    Base subtitle codec class handles general subtitle options. Possible
    parameters are:
      * codec (string) - subtitle codec name (mov_text, subrib, ssa only supported currently)
      * language (string) - language of subtitle stream (3 char code)
      * forced (int) - force subtitles (1 true, 0 false)
      * default (int) - default subtitles (1 true, 0 false)

    Supported subtitle codecs are: null (no subtitle), mov_text
    """

    encoder_options = {
        'codec': str,
        'language': str,
        'forced': int,
        'default': int
    }

    def parse_options(self, opt):
        super(SubtitleCodec, self).parse_options(opt)
        safe = self.safe_options(opt)

        if 'forced' in safe:
            f = safe['forced']
            if f < 0 or f > 1:
                del safe['forced']

        if 'default' in safe:
            d = safe['default']
            if d < 0 or d > 1:
                del safe['default']

        if 'language' in safe:
            l = safe['language']
            if len(l) > 3:
                del safe['language']

        safe = self._codec_specific_parse_options(safe)

        optlist = ['-scodec', self.ffmpeg_codec_name]

        optlist.extend(self._codec_specific_produce_ffmpeg_list(safe))
        return optlist


class VideoCodec(BaseCodec):
    """
    Base video codec class handles general video options. Possible
    parameters are:
      * codec (string) - video codec name
      * bitrate (float) - stream bitrate
      * fps (float) - frames per second
      * max_width (integer) - video width
      * max_height (integer) - video height
      * filters (string) - filters (flip, rotate, etc)
      * sizing_policy (string) - aspect preserval mode; one of:
            ...
      * src_width (int) - source width
      * src_height (int) - source height
      * src_rotate (90) -

    Aspect preserval mode is only used if both source
    and both destination sizes are specified. If source
    dimensions are not specified, aspect settings are ignored.

    If source dimensions are specified, and only one
    of the destination dimensions is specified, the other one
    is calculated to preserve the aspect ratio.

    Supported video codecs are: null (no video), copy (copy directly
    from the source), Theora, H.264/AVC, DivX, VP8, H.263, Flv,
    MPEG-1, MPEG-2.
    """

    encoder_options = {
        'codec': str,
        'bitrate': float,
        'fps': float,
        'pix_fmt': str,
        'max_width': int,
        'max_height': int,
        'sizing_policy': str,
        'src_width': int,
        'src_height': int,
        'src_rotate': int,
        'crop': str,
        'filters': str,
        'autorotate': bool,
        'bufsize': int,
        'flags': str,
        'color_primaries': str,
        'color_trc': str,
        'colorspace': str,
    }

    def _autorotate(self, src_rotate):
        filters = {
            90: "transpose=1",
            180: "transpose=2,transpose=2",
            270: "transpose=2"
        }
        return filters.get(src_rotate)

    def _extend_vf(self, optlist, value):

        if not value:
            return optlist

        if optlist.count('-vf'):
            current_vf = optlist[optlist.index('-vf') + 1]
            new_vf = "{0},{1}".format(current_vf, value)  # append filters to current
            optlist[optlist.index('-vf') + 1] = new_vf
        else:
            optlist.extend(['-vf', value])
        return optlist

    def _div_by_2(self, d):
        return d + 1 if d % 2 else d

    def _aspect_corrections(self, sw, sh, max_width, max_height, sizing_policy):
        if not max_width or not max_height or not sw or not sh:
            return sw, sh, None

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
                return int(sw * factor), max_height, None
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
        the values that you specified for Max Width and Max Height, FFMPEG crops the output video.
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
        this option, FFMPEG does not scale the video up.
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

    def parse_options(self, opt):
        super(VideoCodec, self).parse_options(opt)

        safe = self.safe_options(opt)

        if 'fps' in safe:
            f = safe['fps']
            if f < 1 or f > 120:
                del safe['fps']

        if 'bitrate' in safe:
            br = safe['bitrate']
            if br < 0.1 or br > 200:
                del safe['bitrate']

        if 'bufsize' in safe:
            bufsize = safe['bufsize']
            if bufsize < 1 or bufsize > 10000:
                del safe['bufsize']

        w = h = None

        if 'max_width' in safe:
            w = safe['max_width']
            if w < 16 or w > 4000:
                w = None

        if 'max_height' in safe:
            h = safe['max_height']
            if h < 16 or h > 3000:
                h = None

        crop_width, crop_height = None, None

        if 'crop' in safe:
            try:
                crop_width, crop_height, _, _ = safe['crop'].split(':')
                crop_width, crop_height = int(crop_width), int(crop_height)
            except Exception:
                del safe['crop']
            else:
                if crop_width < 16 or crop_width > 3000:
                    crop_width = None
                if crop_height < 16 or crop_height > 3000:
                    crop_height = None

        if crop_width and crop_height:
            sw = crop_width
            sh = crop_height
        else:
            sw = safe.get('src_width', None)
            sh = safe.get('src_height', None)

        sizing_policy = 'Keep'
        if 'sizing_policy' in safe:
            if safe['sizing_policy'] in ['Fit', 'Fill', 'Stretch', 'Keep', 'ShrinkToFit', 'ShrinkToFill']:
                sizing_policy = safe['sizing_policy']

        w, h, filters = self._aspect_corrections(sw, sh, w, h, sizing_policy)
        w = self._div_by_2(w)
        h = self._div_by_2(h)

        safe['max_width'] = w
        safe['max_height'] = h
        safe['aspect_filters'] = filters

        # swap height and width if vertical rotate
        if safe.get('autorotate') and 'src_rotate' in safe:
            if safe['src_rotate'] in [90, 270]:
                old_w = w
                old_h = h
                safe['max_width'] = w = old_h
                safe['max_height'] = h = old_w

        if w and h:
            safe['aspect'] = '{0}:{1}'.format(w, h)

        safe = self._codec_specific_parse_options(safe)

        #w = safe['max_width']
        #h = safe['max_height']
        filters = safe['aspect_filters']

        # Use the most common pixel format by default. If the selected pixel format can not be selected,
        # ffmpeg select the best pixel format supported by the encoder.
        pix_fmt = safe.get('pix_fmt', 'yuv420p')

        optlist = ['-vcodec', self.ffmpeg_codec_name, '-pix_fmt', pix_fmt]

        if 'fps' in safe:
            optlist.extend(['-r', str(round(safe['fps'], 2))])
        if 'bitrate' in safe:
            optlist.extend(['-vb', str(safe['bitrate']) + 'M'])
        if 'bufsize' in safe:
            optlist.extend(['-bufsize', str(safe['bufsize']) + 'k'])
        if 'flags' in safe:
            optlist.extend(['-flags', str(safe['flags'])])
        if 'color_primaries' in safe:
            optlist.extend(['-color_primaries', str(safe['color_primaries'])])
        if 'color_trc' in safe:
            optlist.extend(['-color_trc', str(safe['color_trc'])])
        if 'colorspace' in safe:
            optlist.extend(['-colorspace', str(safe['colorspace'])])
        if w and h:
            optlist.extend(['-s', '{0}x{1}'.format(w, h)])
            if 'aspect' in safe:
                optlist.extend(['-aspect', '{0}:{1}'.format(w, h)])

        if safe.get('crop'):
            optlist = self._extend_vf(optlist, 'crop={0}'.format(safe['crop']))

        if filters:
            optlist = self._extend_vf(filters)

        if safe.get('autorotate', False) and 'src_rotate' in safe:
            rotate_filter = self._autorotate(safe['src_rotate'])
            optlist = self._extend_vf(optlist, rotate_filter)

        if 'filters' in safe:
            optlist = self._extend_vf(optlist, safe['filters'])

        optlist.extend(self._codec_specific_produce_ffmpeg_list(safe))
        return optlist


class AudioNullCodec(BaseCodec):
    """
    Null audio codec (no audio).
    """
    codec_name = None

    def parse_options(self, opt):
        return ['-an']


class VideoNullCodec(BaseCodec):
    """
    Null video codec (no video).
    """

    codec_name = None

    def parse_options(self, opt):
        return ['-vn']


class SubtitleNullCodec(BaseCodec):
    """
    Null video codec (no video).
    """

    codec_name = None

    def parse_options(self, opt):
        return ['-sn']


class AudioCopyCodec(BaseCodec):
    """
    Copy audio stream directly from the source.
    """
    codec_name = 'copy'

    def parse_options(self, opt):
        return ['-acodec', 'copy']


class VideoCopyCodec(BaseCodec):
    """
    Copy video stream directly from the source.
    """
    codec_name = 'copy'

    def parse_options(self, opt):
        return ['-vcodec', 'copy']


class SubtitleCopyCodec(BaseCodec):
    """
    Copy subtitle stream directly from the source.
    """
    codec_name = 'copy'

    def parse_options(self, opt):
        return ['-scodec', 'copy']


# Audio Codecs
class VorbisCodec(AudioCodec):
    """
    Vorbis audio codec.
    @see http://ffmpeg.org/trac/ffmpeg/wiki/TheoraVorbisEncodingGuide
    """
    codec_name = 'vorbis'
    ffmpeg_codec_name = 'libvorbis'
    encoder_options = AudioCodec.encoder_options.copy()
    encoder_options.update({
        'quality': int,  # audio quality. Range is 0-10(highest quality)
        # 3-6 is a good range to try. Default is 3
    })

    def _codec_specific_produce_ffmpeg_list(self, safe):
        optlist = []
        if 'quality' in safe:
            optlist.extend(['-qscale:a', safe['quality']])
        return optlist


class AacCodec(AudioCodec):
    """
    AAC audio codec.
    """
    codec_name = 'aac'
    ffmpeg_codec_name = 'aac'
    aac_experimental_enable = ['-strict', 'experimental']

    def _codec_specific_produce_ffmpeg_list(self, safe):
        return self.aac_experimental_enable


class FdkAacCodec(AudioCodec):
    """
    AAC audio codec.
    """
    codec_name = 'libfdk_aac'
    ffmpeg_codec_name = 'libfdk_aac'


class Ac3Codec(AudioCodec):
    """
    AC3 audio codec.
    """
    codec_name = 'ac3'
    ffmpeg_codec_name = 'ac3'


class FlacCodec(AudioCodec):
    """
    FLAC audio codec.
    """
    codec_name = 'flac'
    ffmpeg_codec_name = 'flac'


class DtsCodec(AudioCodec):
    """
    DTS audio codec.
    """
    codec_name = 'dts'
    ffmpeg_codec_name = 'dts'


class Mp3Codec(AudioCodec):
    """
    MP3 (MPEG layer 3) audio codec.
    """
    codec_name = 'mp3'
    ffmpeg_codec_name = 'libmp3lame'


class Mp2Codec(AudioCodec):
    """
    MP2 (MPEG layer 2) audio codec.
    """
    codec_name = 'mp2'
    ffmpeg_codec_name = 'mp2'


# Video Codecs
class TheoraCodec(VideoCodec):
    """
    Theora video codec.
    @see http://ffmpeg.org/trac/ffmpeg/wiki/TheoraVorbisEncodingGuide
    """
    codec_name = 'theora'
    ffmpeg_codec_name = 'libtheora'
    encoder_options = VideoCodec.encoder_options.copy()
    encoder_options.update({
        'quality': int,  # audio quality. Range is 0-10(highest quality)
        # 5-7 is a good range to try (default is 200k bitrate)
    })

    def _codec_specific_produce_ffmpeg_list(self, safe):
        optlist = []
        if 'quality' in safe:
            optlist.extend(['-qscale:v', safe['quality']])
        return optlist


class H264Codec(VideoCodec):
    """
    H.264/AVC video codec.
    @see http://ffmpeg.org/trac/ffmpeg/wiki/x264EncodingGuide
    """
    codec_name = 'h264'
    ffmpeg_codec_name = 'libx264'
    encoder_options = VideoCodec.encoder_options.copy()
    encoder_options.update({
        'preset': str,  # common presets are ultrafast, superfast, veryfast,
        # faster, fast, medium(default), slow, slower, veryslow
        'quality': int,  # constant rate factor, range:0(lossless)-51(worst)
        # default:23, recommended: 18-28
        # http://mewiki.project357.com/wiki/X264_Settings#profile
        'profile': str,  # default: not-set, for valid values see above link
        'tune': str,  # default: not-set, for valid values see above link
        'level': str,  # The H.264 level that you want to use for the output video
        'max_reference_frames': int,  # reference frames
        'max_rate': str,
        'max_frames_between_keyframes': int,
        'qmin': int,
        'qcomp': float,
        'keyint_min': int,
        'subq': int,
        'b-pyramid': int,
        'trellis': int,
        'x264opts': str,
    })

    def _codec_specific_produce_ffmpeg_list(self, safe):
        optlist = []
        if 'preset' in safe:
            optlist.extend(['-preset', safe['preset']])
        if 'quality' in safe:
            optlist.extend(['-crf', str(safe['quality'])])
        if 'profile' in safe:
            optlist.extend(['-profile:v', safe['profile']])
        if 'tune' in safe:
            optlist.extend(['-tune', safe['tune']])
        if 'level' in safe:
            optlist.extend(['-level', safe['level']])
        if 'max_reference_frames' in safe:
            optlist.extend(['-refs', str(safe['max_reference_frames'])])
        if 'max_rate' in safe:
            optlist.extend(['-maxrate', str(safe['max_rate'])])
        if 'max_frames_between_keyframes' in safe:
            optlist.extend(['-g', str(safe['max_frames_between_keyframes'])])
        if 'qmin' in safe:
            optlist.extend(['-qmin', str(safe['qmin'])])
        if 'qcomp' in safe:
            optlist.extend(['-qcomp', str(safe['qcomp'])])
        if 'keyint_min' in safe:
            optlist.extend(['-keyint_min', str(safe['keyint_min'])])
        if 'subq' in safe:
            optlist.extend(['-subq', str(safe['subq'])])
        if 'b-pyramid' in safe:
            optlist.extend(['-b-pyramid', str(safe['b-pyramid'])])
        if 'trellis' in safe:
            optlist.extend(['-trellis', str(safe['trellis'])])
        if 'x264opts' in safe:
            optlist.extend(['-x264opts', str(safe['x264opts'])])

        return optlist


class DivxCodec(VideoCodec):
    """
    DivX video codec.
    """
    codec_name = 'divx'
    ffmpeg_codec_name = 'mpeg4'


class Vp8Codec(VideoCodec):
    """
    Google VP8 video codec.
    """
    codec_name = 'vp8'
    ffmpeg_codec_name = 'libvpx'


class H263Codec(VideoCodec):
    """
    H.263 video codec.
    """
    codec_name = 'h263'
    ffmpeg_codec_name = 'h263'


class FlvCodec(VideoCodec):
    """
    Flash Video codec.
    """
    codec_name = 'flv'
    ffmpeg_codec_name = 'flv'


class Ffv1Codec(VideoCodec):
    """
    FFV1 Video codec.
    """
    codec_name = 'ffv1'
    ffmpeg_codec_name = 'ffv1'
    encoder_options = VideoCodec.encoder_options.copy()
    encoder_options.update({
        'level': int,  # 1, 3. Select which FFV1 version to use.
        'coder': int,  # 0=Golomb-Rice, 1=Range Coder,
                       # 2=Range Coder(with custom state transition table)
        'context': int,  # 0=small, 1=large
        'g': int,  # GOP size, >= 1. For archival use, GOP-size should be "1".
        'slices': int,  # 4, 6, 9, 12, 16, 24, 30.
                        # Each frame is split into this number of slices.
                        # This affects multithreading performance, as well as filesize:
                        # Increasing the number of slices might speed up
                        # performance, but also increases the filesize.
        'slicecrc': int,  # Error correction/detection, 0=off, 1=on.
                          # Enabling this option adds CRC information to each slice.
                          # This makes it possible for a decoder to detect
                          # errors in the bitstream, rather than blindly
                          # decoding a broken slice.
        'threads': int,  # The number of threads to use while processing.
                         # Adjust this to match how many of your available
                         # CPU cores you want to use.
    })

    def _codec_specific_produce_ffmpeg_list(self, safe):
        optlist = []
        if 'level' in safe:
            optlist.extend(['-level', str(safe['level'])])
        if 'coder' in safe:
            optlist.extend(['-coder', str(safe['coder'])])
        if 'context' in safe:
            optlist.extend(['-context', str(safe['context'])])
        if 'g' in safe:
            optlist.extend(['-g', str(safe['g'])])
        if 'slices' in safe:
            optlist.extend(['-slices', str(safe['slices'])])
        if 'slicecrc' in safe:
            optlist.extend(['-slicecrc', str(safe['slicecrc'])])
        if 'threads' in safe and safe['threads'] > 0:
            optlist.extend(['-threads', str(safe['threads'])])

        return optlist


class MpegCodec(VideoCodec):
    """
    Base MPEG video codec.
    """
    # Workaround for a bug in ffmpeg in which aspect ratio
    # is not correctly preserved, so we have to set it
    # again in vf; take care to put it *before* crop/pad, so
    # it uses the same adjusted dimensions as the codec itself
    # (pad/crop will adjust it further if neccessary)
    def _codec_specific_parse_options(self, safe):
        w = safe['max_width']
        h = safe['max_height']

        if w and h:
            filters = safe['aspect_filters']
            tmp = 'aspect=%d:%d' % (w, h)

            if filters is None:
                safe['aspect_filters'] = tmp
            else:
                safe['aspect_filters'] = tmp + ',' + filters

        return safe


class Mpeg1Codec(MpegCodec):
    """
    MPEG-1 video codec.
    """
    codec_name = 'mpeg1'
    ffmpeg_codec_name = 'mpeg1video'


class Mpeg2Codec(MpegCodec):
    """
    MPEG-2 video codec.
    """
    codec_name = 'mpeg2'
    ffmpeg_codec_name = 'mpeg2video'


# Subtitle Codecs
class MOVTextCodec(SubtitleCodec):
    """
    mov_text subtitle codec.
    """
    codec_name = 'mov_text'
    ffmpeg_codec_name = 'mov_text'


class SSA(SubtitleCodec):
    """
    SSA (SubStation Alpha) subtitle.
    """
    codec_name = 'ass'
    ffmpeg_codec_name = 'ass'


class SubRip(SubtitleCodec):
    """
    SubRip subtitle.
    """
    codec_name = 'subrip'
    ffmpeg_codec_name = 'subrip'


class DVBSub(SubtitleCodec):
    """
    DVB subtitles.
    """
    codec_name = 'dvbsub'
    ffmpeg_codec_name = 'dvbsub'


class DVDSub(SubtitleCodec):
    """
    DVD subtitles.
    """
    codec_name = 'dvdsub'
    ffmpeg_codec_name = 'dvdsub'


audio_codec_list = [
    AudioNullCodec, AudioCopyCodec, VorbisCodec, AacCodec, Mp3Codec, Mp2Codec,
    FdkAacCodec, Ac3Codec, DtsCodec, FlacCodec
]

video_codec_list = [
    VideoNullCodec, VideoCopyCodec, TheoraCodec, H264Codec,
    DivxCodec, Vp8Codec, H263Codec, FlvCodec, Ffv1Codec, Mpeg1Codec,
    Mpeg2Codec
]

subtitle_codec_list = [
    SubtitleNullCodec, SubtitleCopyCodec, MOVTextCodec, SSA, SubRip, DVDSub,
    DVBSub
]
