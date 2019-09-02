#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

"""
    create project on Google Developer console
    Add Youtube Data API v3 to it
    Create credentials (Other non-UI, Public Data)
"""

import os
import json
import shutil
from pathlib import Path
from functools import partial

import jinja2
import iso639
import youtube_dl
from dateutil import parser as dt_parser

from .zim import ZimInfo, make_zim_file
from .utils import (
    clean_text,
    resize_image,
    load_json,
    save_json,
    get_slug,
    save_file,
    get_colors,
    is_hex_color,
)
from .youtube import (
    get_channel_json,
    credentials_ok,
    Playlist,
    get_channel_playlists_json,
    get_videos_json,
    get_videos_authors_info,
    save_channel_branding,
)
from .converter import hook_youtube_dl_ffmpeg
from .constants import logger, ROOT_DIR, CHANNEL, PLAYLIST, USER


class Youtube2Zim(object):
    def __init__(
        self,
        collection_type,
        youtube_id,
        api_key,
        video_format,
        low_quality,
        all_subtitles,
        output_dir,
        no_zim,
        fname,
        debug,
        keep_build_dir,
        skip_download,
        youtube_store,
        lang,
        tags,
        title=None,
        description=None,
        creator=None,
        publisher=None,
        name=None,
        profile_image=None,
        banner_image=None,
        main_color=None,
        secondary_color=None,
    ):
        # data-retrieval info
        self.collection_type = collection_type
        self.youtube_id = youtube_id
        self.api_key = api_key

        # video-encoding info
        self.video_format = video_format
        self.low_quality = low_quality

        # options & zim params
        self.all_subtitles = all_subtitles
        self.fname = Path(fname)
        self.lang = lang
        self.tags = tags
        self.title = title
        self.description = description
        self.creator = creator
        self.publisher = publisher
        self.name = name
        self.profile_image = profile_image
        self.banner_image = banner_image
        self.main_color = main_color
        self.secondary_color = secondary_color

        # process-related
        self.output_dir = Path(output_dir)
        self.playlists = []
        self.videos_ids = []
        self.main_channel_id = None  # use for branding

        # debug/devel options
        self.no_zim = no_zim
        self.debug = debug
        self.keep_build_dir = keep_build_dir
        self.skip_download = skip_download

        self.build_dir = self.output_dir.joinpath("build")

        # store ZIM-related info
        self.zim_info = ZimInfo(
            language=lang,
            tags=tags,
            title=title,
            description=description,
            creator=creator,
            publisher=publisher,
            name=name,
        )

        # update youtube credentials store
        youtube_store.update(
            build_dir=self.build_dir, api_key=self.api_key, cache_dir=self.cache_dir
        )

    @property
    def root_dir(self):
        return ROOT_DIR

    @property
    def templates_dir(self):
        return self.root_dir.joinpath("templates")

    @property
    def assets_src_dir(self):
        return self.templates_dir.joinpath("assets")

    @property
    def assets_dir(self):
        return self.build_dir.joinpath("assets")

    @property
    def channels_dir(self):
        return self.build_dir.joinpath("channels")

    @property
    def cache_dir(self):
        return self.build_dir.joinpath("cache")

    @property
    def videos_dir(self):
        return self.build_dir.joinpath("videos")

    @property
    def profile_path(self):
        return self.build_dir.joinpath("profile.jpg")

    @property
    def banner_path(self):
        return self.build_dir.joinpath("banner.jpg")

    @property
    def is_user(self):
        return self.collection_type == USER

    @property
    def is_channel(self):
        return self.collection_type == CHANNEL

    @property
    def is_playlist(self):
        return self.collection_type == PLAYLIST

    def run(self):
        """ execute the scrapper step by step """
        logger.info(
            f"starting youtube scraper for {self.collection_type}#{self.youtube_id}"
        )

        logger.info("preparing build folder at {}".format(self.build_dir.resolve()))
        if not self.keep_build_dir and self.build_dir.exists():
            shutil.rmtree(self.cache_dir, ignore_errors=True)
            shutil.rmtree(self.build_dir)
        self.make_build_folder()

        logger.info("testing Youtbe credentials")
        if not credentials_ok():
            raise ValueError("Unable to connect to Youtube API v3. check `API_KEY`.")

        # fail early if supplied branding files are missing
        self.check_branding_values()

        logger.info("compute playlists list to retrieve")
        self.extract_playlists()

        logger.info(
            ".. {} playlists:\n   {}".format(
                len(self.playlists),
                "\n   ".join([p.playlist_id for p in self.playlists]),
            )
        )

        logger.info("compute list of videos")
        self.extract_videos_list()
        logger.info(".. {} videos.".format(len(self.videos_ids)))

        # download videos (and recompress)
        logger.info("downloading all videos, subtitles and thumbnails")
        logger.info(f"  format: {self.video_format}")
        logger.info(f"  recompress: {self.low_quality}")
        logger.info(f"  generated-subtitles: {self.all_subtitles}")
        if not self.skip_download:
            self.download_video_files()

        logger.info("retrieve channel-info for all videos (author details)")
        get_videos_authors_info(self.videos_ids)

        logger.info("download all author's profile pictures")
        self.download_authors_branding()

        logger.info("update general metadata")
        self.update_metadata()

        logger.info("creating HTML files")
        self.make_html_files()

        # make zim file
        if not self.no_zim:
            logger.info("building ZIM file")
            make_zim_file(self.build_dir, self.output_dir, self.fname, self.zim_info)
            logger.info("removing HTML folder")
            # shutil.rmtree(self.build_dir, ignore_errors=True)

        logger.info("all done!")

    def make_build_folder(self):
        """ prepare build folder before we start downloading data """

        # create build folder
        os.makedirs(self.build_dir, exist_ok=True)

        # copy assets
        if self.assets_dir.exists():
            shutil.rmtree(self.assets_dir)
        shutil.copytree(self.assets_src_dir, self.assets_dir)

        # cache folder to store youtube-api results
        self.cache_dir.mkdir(exist_ok=True)

        # make videos placeholder
        self.videos_dir.mkdir(exist_ok=True)

        # make channels placeholder (profile files)
        self.channels_dir.mkdir(exist_ok=True)

    def check_branding_values(self):
        """ checks that user-supplied images and colors are valid (so to fail early)

            Images are checked for existence or downloaded then resized
            Colors are check for validity """

        # skip this step if none of related values were supplied
        if not sum(
            [
                bool(x)
                for x in (
                    self.profile_image,
                    self.banner_image,
                    self.main_color,
                    self.secondary_color,
                )
            ]
        ):
            return
        logger.info("checking your branding files and values")
        if self.profile_image:
            if self.profile_image.startswith("http"):
                save_file(self.profile_image, self.profile_path)
            else:
                if not self.profile_image.exists():
                    raise IOError(
                        f"--profile image could not be found: {self.profile_image}"
                    )
                shutil.move(self.profile_image, self.profile_path)
            resize_image(self.profile_path, width=100, height=100, method="thumbnail")
        if self.banner_image:
            if self.banner_image.startswith("http"):
                save_file(self.banner_image, self.banner_path)
            else:
                if not self.banner_image.exists():
                    raise IOError(
                        f"--banner image could not be found: {self.banner_image}"
                    )
                shutil.move(self.banner_image, self.banner_path)
            resize_image(self.banner_path, width=1060, height=175, method="thumbnail")

        if self.main_color and not is_hex_color(self.main_color):
            raise ValueError(
                f"--main-color is not a valid hex color: {self.main_color}"
            )

        if self.secondary_color and not is_hex_color(self.secondary_color):
            raise ValueError(
                f"--secondary_color-color is not a valid hex color: {self.secondary_color}"
            )

    def extract_playlists(self):
        """ prepare a list of Playlist from user request

            USER: we fetch the hidden channel associate to it
            CHANNEL (and USER): we grab all playlists + `uploads` playlist
            PLAYLIST: we retrieve from the playlist Id(s) """

        if self.is_user or self.is_channel:
            if self.is_user:
                # youtube_id is a Username, fetch actual channelId through channel
                channel_json = get_channel_json(self.youtube_id, for_username=True)
            else:
                # youtube_id is a channelId
                channel_json = get_channel_json(self.youtube_id)

            self.main_channel_id = channel_json["id"]

            # retrieve list of playlists for that channel
            playlist_ids = [
                p["id"] for p in get_channel_playlists_json(self.main_channel_id)
            ]
            # we always include uploads playlist (contains everything)
            playlist_ids += [
                channel_json["contentDetails"]["relatedPlaylists"]["uploads"]
            ]
        elif self.is_playlist:
            playlist_ids = self.youtube_id.split(",")
            self.main_channel_id = Playlist.from_id(playlist_ids[0]).creator_id
        else:
            raise NotImplementedError("unsupported collection_type")

        self.playlists = [
            Playlist.from_id(playlist_id) for playlist_id in list(set(playlist_ids))
        ]

    def extract_videos_list(self):

        all_videos = load_json(self.cache_dir, "videos")
        if all_videos is None:
            all_videos = {}
            # videos_ids = []
            # we only return video_ids that we'll use later on. per-playlist JSON stored
            for playlist in self.playlists:
                all_videos.update(
                    {
                        v["contentDetails"]["videoId"]: v
                        for v in get_videos_json(playlist.playlist_id)
                    }
                )
                # videos_ids += [
                #     v["contentDetails"]["videoId"]
                #     for v in get_videos_json(playlist.playlist_id)
                # ]

            # self.videos_ids = list(set(videos_ids))
            save_json(self.cache_dir, "videos", all_videos)
        self.videos_ids = all_videos.keys()

    def download_video_files(self):

        options = {
            "cachedir": self.videos_dir,
            "writethumbnail": True,
            "write_all_thumbnails": True,
            "writesubtitles": True,
            "subtitlesformat": "vtt",
            "keepvideo": False,
            "external_downloader": "aria2c",
            "external_downloader_args": None,
            "outtmpl": str(self.videos_dir.joinpath("%(id)s", "video.%(ext)s")),
            "preferredcodec": self.video_format,
            "format": self.video_format,
        }
        if self.all_subtitles:
            options.update({"writeautomaticsub": True, "allsubtitles": True})

        if self.low_quality:
            options.update(
                {
                    "prefer_ffmpeg": True,
                    "progress_hooks": [
                        partial(hook_youtube_dl_ffmpeg, self.video_format)
                    ],
                }
            )
        with youtube_dl.YoutubeDL(options) as ydl:
            ydl.download(self.videos_ids)

        # resize thumbnails. we use max width:248px
        for video_id in self.videos_ids:
            resize_image(
                self.videos_dir.joinpath(video_id, "video.jpg"),
                width=248,
                height=187,
                method="cover",
            )

    def download_authors_branding(self):
        videos_channels_json = load_json(self.cache_dir, "videos_channels")
        uniq_channel_ids = list(
            set([chan["channelId"] for chan in videos_channels_json.values()])
        )
        for channel_id in uniq_channel_ids:
            save_channel_branding(self.channels_dir, channel_id, save_banner=False)

    def update_metadata(self):
        # we use title, description, profile and banner of channel/user
        # or channel of first playlist
        main_channel_json = get_channel_json(self.main_channel_id)
        save_channel_branding(self.channels_dir, self.main_channel_id, save_banner=True)

        # if a single playlist was requested, use if for names;
        # otherwise, use main_channel's details.
        auto_title = (
            self.playlists[0].title
            if self.is_playlist and len(self.playlists) == 1
            else main_channel_json["snippet"]["title"].strip()
        )
        auto_description = (
            clean_text(self.playlists[0].description)
            if self.is_playlist and len(self.playlists) == 1
            else clean_text(main_channel_json["snippet"]["description"])
        )
        self.title = self.title or auto_title
        self.description = self.description or auto_description

        if self.creator is None:
            if self.is_playlist:
                self.creator = "Youtube Channels"
            else:
                self.creator = "Youtube Channel “{}”".format(
                    main_channel_json["snippet"]["title"]
                )
        self.publisher = self.publisher or "Kiwix"

        self.name = "youtube-{ident}_{lang}_all"
        self.tags = self.tags or ["youtube"]
        if "_videos:yes" not in self.tags:
            self.tags.append("_videos:yes")

        self.zim_info.update(
            title=self.title,
            description=self.description,
            creator=self.creator,
            publisher=self.publisher,
            name=self.name,
            tags=self.tags,
        )

        # copy our main_channel branding into /(profile|banner).jpg if not supplied
        if not self.profile_path.exists():
            shutil.copy(
                self.channels_dir.joinpath(self.main_channel_id, "profile.jpg"),
                self.profile_path,
            )
        if not self.banner_path.exists():
            shutil.copy(
                self.channels_dir.joinpath(self.main_channel_id, "banner.jpg"),
                self.banner_path,
            )

        # set colors from images if not supplied
        if self.main_color is None or self.secondary_color is None:
            profile_main, profile_secondary = get_colors(self.profile_path)
        self.main_color = self.main_color or profile_main
        self.secondary_color = self.secondary_color or profile_secondary

        resize_image(
            self.profile_path,
            width=48,
            height=48,
            method="thumbnail",
            to=self.build_dir.joinpath("favicon.jpg"),
        )

    def make_html_files(self):
        """ make up HTML structure to read the content

        /home.html                                  Homepage

        for each video:
            - <slug-title>.html                     HTML article
            - videos/<videoId>/video.<ext>          video file
            - videos/<videoId>/video.<lang>.vtt     subtititle(s)
            - videos/<videoId>/video.jpg            template
        """

        def get_subtitles(video_id):
            video_dir = self.videos_dir.joinpath(video_id)
            languages = [
                x.stem.split(".")[1]
                for x in video_dir.iterdir()
                if x.is_file() and x.name.endswith(".vtt")
            ]
            non_iso_langs = {
                "zh-Hans": {
                    "code": "zh-Hans",
                    "english": "Simplified Chinese",
                    "native": "简化字",
                },
                "zh-Hant": {
                    "code": "zh-Hant",
                    "english": "Traditional Chinese",
                    "native": "正體字",
                },
                "iw": {"code": "iw", "english": "Hebrew", "native": "עברית"},
            }

            return [
                non_iso_langs.get(language)
                if language in non_iso_langs.keys()
                else {
                    "code": language,
                    "english": iso639.to_name(language),
                    "native": iso639.to_native(language),
                }
                for language in languages
            ]

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.templates_dir))
        )

        videos = load_json(self.cache_dir, "videos").values()
        videos_channels = load_json(self.cache_dir, "videos_channels")
        for video in videos:
            video_id = video["contentDetails"]["videoId"]
            title = video["snippet"]["title"]
            slug = get_slug(title)
            description = video["snippet"]["description"].replace("\n", "<br />")
            publication_date = dt_parser.parse(
                video["contentDetails"]["videoPublishedAt"]
            ).strftime("%Y-%m-%d - %H:%M")
            author = videos_channels[video_id]
            subtitles = get_subtitles(video_id)
            video_url = f"https://www.youtube.com/watch?v={video_id}"

            html = env.get_template("article.html").render(
                video_id=video_id,
                video_format=self.video_format,
                author=author,
                title=title,
                description=description,
                date=publication_date,
                subtitles=subtitles,
                url=video_url,
                channel_id=video["snippet"]["channelId"],
                color=self.main_color,
                background_color=self.secondary_color,
            )
            with open(
                self.build_dir.joinpath(f"{slug}.html"), "w", encoding="utf-8"
            ) as fp:
                fp.write(html)

        # build homepage
        html = env.get_template("home.html").render(
            playlists=self.playlists,
            video_format=self.video_format,
            title=self.title,
            description=self.description,
            color=self.main_color,
            background_color=self.secondary_color,
        )
        with open(self.build_dir.joinpath("home.html"), "w", encoding="utf-8") as fp:
            fp.write(html)

        # rewrite app.js including `format`
        with open(self.assets_dir.joinpath("app.js"), "w", encoding="utf-8") as fp:
            fp.write(
                env.get_template("assets/app.js").render(video_format=self.video_format)
            )

        # write list of videos in data.js
        def to_data_js(video):
            return {
                "id": video["contentDetails"]["videoId"],
                "title": video["snippet"]["title"],
                "slug": get_slug(video["snippet"]["title"]),
                "description": video["snippet"]["description"].replace("\n", "<br />"),
                "thumbnail": str(
                    Path("videos").joinpath(
                        video["contentDetails"]["videoId"], "video.jpg"
                    )
                ),
            }

        with open(self.assets_dir.joinpath("data.js"), "w", encoding="utf-8") as fp:
            # write all playlists as they are
            for playlist in self.playlists:
                playlist_videos = load_json(
                    self.cache_dir, f"playlist_{playlist.playlist_id}_videos"
                )
                playlist_videos.sort(key=lambda v: v["snippet"]["position"])

                fp.write(
                    "var json_{slug} = {json_str};\n".format(
                        slug=playlist.slug,
                        json_str=json.dumps(
                            list(map(to_data_js, playlist_videos)), indent=4
                        ),
                    )
                )

            # # write another playlist containing all videos
            # # ordered by publication date DESC
            # all_videos = sorted(
            #     videos,
            #     key=lambda v: dt_parser.parse(v["snippet"]["publishedAt"]),
            #     reverse=True,
            # )
            # fp.write(
            #     "var json_{slug} = {json_str};\n".format(
            #         slug="all",
            #         json_str=json.dumps(list(map(to_data_js, all_videos)), indent=4),
            #     )
            # )
