"""给页面播放器 mpegts.js 打 Evo 的补丁：Twitch 那种没对齐到帧开头的 H.264 PES，把开头那截接回上一帧（不然画面拖影、重影）。

2026-11-26 查出来的：Twitch 的 TS 里约两成的视频 PES 不是从一帧的开头开始（data_alignment_indicator=0），开头一截是上一帧
最后一个 NAL 的尾巴。mpegts.js 1.8.0（到 1.8.2 都一样）把每个 PES 当成完整的一帧、从第一个起始码切起，那截尾巴被丢掉，
上一帧缺了末尾，画面一路拖影到下一个关键帧（每 2 秒一个）。核对的办法：把 mpegts.js 交给浏览器的视频数据截下来用 ffmpeg 解，
没打补丁时几乎每帧报错、1919 帧只有 25 帧和原画一样；打了补丁 0 错误、逐帧和原画一样（见 docs/底座.md"看直播"的 Twitch）。

用法：python tools/mpegts_patch.py [mpegts.js 的路径]   （默认 panel/static/vendor/mpegts.js；就地改，打过的不再打）
升级 mpegts.js 以后要再运行一次（补丁按 1.8.0 压缩后的原文找位置，版本变了找不到会直接报错，不会乱改）。
"""
import sys
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "panel" / "static" / "vendor" / "mpegts.js"
MARK = "EVO-PATCH"

# parseH264Payload 的开头：先看这一包开头到第一个起始码有几个字节（EVl），有的话接回上一包留着的那一帧（evo_h264_held_）
# 最后一个 NAL 的末尾，再把那一帧交出去；整包都没有起始码（都是上一帧的尾巴）就接上、接着留着
OLD = ('t.prototype.parseH264Payload=function(e,t,i,n,a){for(var o=new U(e),s=null,d=[],_=0,c=!1;null!=(s=o.readNextNaluPayload());){')
NEW = ('t.prototype.parseH264Payload=function(e,t,i,n,a){'
       'var EVh=this.evo_h264_held_,EVl=e.byteLength;for(var EVk=0;EVk+2<e.byteLength;EVk++)if(0===e[EVk]&&0===e[EVk+1]&&1===e[EVk+2]){EVl=EVk>0&&0===e[EVk-1]?EVk-1:EVk;break}'
       'if(EVh){if(EVl>0){var EVu=EVh.units[EVh.units.length-1],EVo=EVu.data,EVd=new Uint8Array(EVo.byteLength+EVl);EVd.set(EVo,0),EVd.set(e.subarray(0,EVl),EVo.byteLength),new DataView(EVd.buffer).setUint32(0,EVd.byteLength-4),EVu.data=EVd,EVh.length+=EVl}'
       'if(EVl>=e.byteLength)return;this.evo_h264_held_=null,this.video_track_.samples.push(EVh),this.video_track_.length+=EVh.length}'
       'for(var o=new U(e),s=null,d=[],_=0,c=!1;null!=(s=o.readNextNaluPayload());){')
# parseH264Payload 的结尾：解出来的这一帧不马上交出去，先留着（等下一包开头那截）
OLD2 = ('var u=Math.floor(t/this.timescale_),f=Math.floor(i/this.timescale_);if(d.length){var m=this.video_track_,g={units:d,length:_,isKeyframe:c,dts:f,pts:u,cts:u-f,file_position:n};m.samples.push(g),m.length+=_}},t.prototype.parseH265Payload')
NEW2 = ('var u=Math.floor(t/this.timescale_),f=Math.floor(i/this.timescale_);if(d.length){var g={units:d,length:_,isKeyframe:c,dts:f,pts:u,cts:u-f,file_position:n};this.evo_h264_held_=g}},t.prototype.parseH265Payload')
HEADER = ("/* mpegts.js 1.8.0（Apache-2.0，见 mpegts.LICENSE.txt）。Evo 改过这个文件（2026-11-26，EVO-PATCH，tools/mpegts_patch.py；搜 EVh）：\n"
          "   MPEG-TS 里 H.264 的 PES 没对齐到帧开头时（data_alignment_indicator=0，Twitch 的流约两成是这样），原版把开头到第一个\n"
          "   起始码的那截丢掉，那是上一帧最后一个 NAL 的尾巴，于是上一帧缺了末尾、画面一路拖影到下一个关键帧。改成：解出来的一帧\n"
          "   先留着，下一包开头那截接回它最后一个 NAL 再交出去（多等一包，约 16 毫秒）。对齐的流（B站）那截长度是 0，照旧。 */\n")


class PatchError(Exception):
    """打不了补丁：已经打过、或者 mpegts.js 的版本变了找不到要改的地方。"""


def patch(text):
    """mpegts.js 的原文 → 打了补丁的原文。打过的、找不到要改的地方抛 PatchError（一处都不改）。"""
    if MARK in text:
        raise PatchError("已经打过补丁了")
    if text.count(OLD) != 1:
        raise PatchError("找不到 parseH264Payload 的开头（mpegts.js 版本变了？要照新版本重新写补丁）")
    if text.count(OLD2) != 1:
        raise PatchError("找不到 parseH264Payload 的结尾（mpegts.js 版本变了？要照新版本重新写补丁）")
    return HEADER + text.replace(OLD, NEW).replace(OLD2, NEW2)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    try:
        path.write_text(patch(path.read_text(encoding="utf-8")), encoding="utf-8")
    except PatchError as e:
        raise SystemExit(str(e)) from None
    print(f"打好了：{path}")
