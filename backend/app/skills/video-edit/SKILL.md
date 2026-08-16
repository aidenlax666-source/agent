---
name: video-edit
description: 视频剪辑与处理：裁剪、合并、加字幕、配背景音乐、转码压缩。使用 FFmpeg 命令行。
keywords: 视频,剪辑,裁剪,合并,拼接,字幕,背景音乐,配音,转码,压缩,mp4,avi,mov,mkv,flv,webm,视频处理,剪视频,截取
---

# 视频剪辑技能指南（FFmpeg）

本任务属于「视频剪辑」技能，请严格按本指南操作。

## 工具
- 用 FFmpeg 命令行处理视频。
- 必须先检测：`shutil.which("ffmpeg")`，如果找不到，输出 `[ERROR] ffmpeg 未安装，请运行 winget install ffmpeg 或到 ffmpeg.org 下载后重试` 并结束。
- 执行命令一律用 `subprocess.run([...命令列表...], capture_output=True, text=True)`，**不要用 shell=True**（防中文路径/编码问题）。

## 输入
- 用户上传的视频/音频/字幕文件在 uploads 目录，完整路径写在需求或上下文里，直接引用。
- 没有输入文件时，可用 ffmpeg 内置测试源生成：`ffmpeg -f lavfi -i testsrc=duration=5:size=640x360:rate=25 out.mp4`

## 常用命令模板
- 裁剪片段：`ffmpeg -i 输入.mp4 -ss 00:00:10 -t 30 -c copy 输出.mp4`
- 拼接合并：先写 concat.txt（每行 `file 'xxx.mp4'`），再 `ffmpeg -f concat -safe 0 -i concat.txt -c copy 输出.mp4`
- 加字幕：`ffmpeg -i 输入.mp4 -vf subtitles=字幕.srt 输出.mp4`
- 配背景音乐：`ffmpeg -i 视频.mp4 -i 音乐.mp3 -c:v copy -c:a aac -shortest 输出.mp4`
- 提取音频：`ffmpeg -i 视频.mp4 -vn -c:a mp3 输出.mp3`
- 压缩转码：`ffmpeg -i 输入.mp4 -crf 23 -preset fast 输出.mp4`
- 生成测试视频：`ffmpeg -f lavfi -i testsrc=duration=5:size=640x360:rate=25 输出.mp4`

## 输出要求
- 输出文件写到**当前工作目录**，命名 `video_xxx.mp4`（video_ 前缀）
- 执行成功后打印 `[OUTPUT_FILE] <完整路径>` 或 `[OUTPUT_URL] /video_xxx.mp4`
- 失败打印 `[ERROR] <原因>`；命令非零退出码时打印它的 stderr
