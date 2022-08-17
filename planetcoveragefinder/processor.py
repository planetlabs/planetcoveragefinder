# Copyright 2022 Planet Labs PBC
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from planet import api
from planet.api import downloader
from planet.api.exceptions import APIException
from dateutil.relativedelta import relativedelta
from shapely import geometry
from shapely.errors import ShapelyError
from pyproj import Proj, Transformer, CRS
from shapely.ops import transform
from os.path import basename
import tempfile
import rasterio
import rasterio.mask
from tqdm import tqdm
from waiting import wait, TimeoutExpired

SEARCH_SIZE = 1024
MAX_TILES = 64

def item_metadata_cloudiness(item):
    visible_percent = item["properties"].get("visible_percent")
    if visible_percent:
        return 100 - visible_percent
    else:
        return item["properties"].get("cloud_cover") * 100

class AOI():
    use_status = True

    def __init__(self, fid, geom, position):
        self._id = fid
        self.geom = geom
        self.min_clouds = 100
        self.max_cover = 0
        self.wmts_url = None
        self.xyz_url = None
        if AOI.use_status:
            self.tqdm = tqdm(total=0, position=position+1, bar_format="{}: {}".format(fid, "{desc}"))
        else:
            self.tqdm = tqdm(disable=True)
        self.status("waiting to start")

    @property
    def id(self):
        return self._id

    @property
    def geojson(self):
        return self.geom.__geo_interface__

    def _log(self, level, msg):
        self.tqdm.write("{}: {}".format(self.id, msg))

    def debug(self, msg):
        if AOI.verbosity >= 3:
            self._log("DEBUG", msg)

    def info(self, msg):
        if AOI.verbosity >= 2:
            self._log("INFO ", msg)

    def warn(self, msg):
        if AOI.verbosity >= 1:
            self._log("WARN ", msg)

    def error(self, msg):
        self._log("ERROR", msg)

    def status(self, msg=None):
        if AOI.use_status:
            if msg is None:
                self.status(self._status)
            else:
                self._status = msg
                self.debug(msg)
                self.tqdm.set_description_str(msg)
        else:
            self.tqdm.write("{}: {}".format(self.id, msg))

class Tile():
    def __init__(self, item, geom):
        self.item = item
        self.geom = geometry.shape(geom)
        self.clouds = None
        self.confidence = None

    @property
    def id(self):
        return self.item["id"]

    @property
    def area(self):
        return self.geom.area

    @property
    def geojson(self):
        return self.geom.__geo_interface__

class Processor(object):
    def __init__(self, dates, frame, max_clouds, min_confidence, min_cover, satellite, mask_bands, image_quality, order, email, crs, download, bundle):
        self.dates = dates
        self.frame = frame - 1
        self.max_clouds = max_clouds
        self.min_confidence = min_confidence
        self.min_cover = min_cover
        self.satellite = satellite
        self.mask_bands = mask_bands
        self.image_quality = image_quality
        self.order = order
        self.email = email
        self.crs = crs
        self.download = download
        self.bundle = bundle
        self.client = api.ClientV1()

    def search_date(self, aoi, date, geom_filter):
        item_types = ["PSScene"]
        d1 = date + relativedelta(days=1)
        d2 = date - relativedelta(days=self.frame)
        d1_filter = api.filters.date_range("acquired", lt=d1)
        d2_filter = api.filters.date_range("acquired", gte=d2)
        if self.frame == 0:
            aoi.status("searching for coverage on {}".format(d2.strftime("%Y-%m-%d")))
        else:
            aoi.status("searching for coverage between {} and {}".format(d2.strftime("%Y-%m-%d"), date.strftime("%Y-%m-%d")))
        filters = [d1_filter, d2_filter, geom_filter]
        if self.satellite:
            sat_filter = api.filters.string_filter("instrument", self.satellite)
            filters.append(sat_filter)
        if self.image_quality == "standard":
            quality_filter = api.filters.string_filter("quality_category", self.image_quality)
            filters.append(quality_filter)
        search_filters = api.filters.and_filter(*filters)
        req = api.filters.build_search_request(search_filters, item_types)
        res = self.client.quick_search(req, sort="acquired desc")
        if self.image_quality == "preferential":
            return sorted(res.items_iter(SEARCH_SIZE), key=lambda item: (item["properties"]["quality_category"], item_metadata_cloudiness(item)), reverse=False) # return results sorted by quality then by cloudfreeness
        else:
            return sorted(res.items_iter(SEARCH_SIZE), key=lambda item: item_metadata_cloudiness(item), reverse=False) # return results sorted by cloudfreeness

    def build_mosaic(self, res, aoi):
        # start with an empty coverage of the target AOI
        mosaic = geometry.MultiPolygon()
        tiles = []
        for item in res:
            footprint = geometry.shape(item["geometry"])
            geom = footprint.intersection(aoi.geom) # clip to aoi

            # if the image falls inside aoi but is at least partially outside the current mosaic,
            # then we need this image (or at least part of it)
            if aoi.geom.intersects(geom) and not mosaic.contains(geom):
                new_geom = geom.difference(mosaic)
                aoi.debug("adding scene {} to coverage".format(item["id"]))
                if new_geom.is_empty:
                    continue
                mosaic = mosaic.union(footprint)
                tile = Tile(item, new_geom)
                tiles.append(tile)
                if mosaic.covers(aoi.geom):
                    return tiles, aoi.geom, 100
                if MAX_TILES < len(tiles):
                    break
            else:
                aoi.debug("did not use scene {} in coverage".format(item["id"]))
        mosaic = mosaic.intersection(aoi.geom)
        cover = int(mosaic.area / aoi.geom.area * 100)
        aoi.debug("added {} scenes to coverage".format(len(tiles)))
        aoi.info("coverage is {}%, minimum acceptable is {}%".format(cover, self.min_cover))
        if self.min_cover <= cover:
            return tiles, mosaic, cover
        return [], None, cover

    def udm1_analysis(self, tile, img):
            clear = (img & 1).sum()
            cloud = (img & 2).sum()
            return clear, cloud, 0

    def udm2_analysis(self, tile, img):
            clear = (img[0] == 1).sum()
            cloud = 0
            for band in [1, 2, 3, 4, 5]:
                data = img[band]
                if band in self.mask_bands:
                    cloud += (data == 1).sum()
                else:
                    clear += (data == 1).sum()
            confidence = img[6][img[6] != 255].mean() # exclude masked pixel values of 255
            return clear, cloud, confidence

    def get_udm_clouds(self, filename, tile, udm_analysis):
        with rasterio.open(filename) as src:
            outmeta = src.meta
            project = Transformer.from_proj(
                Proj(CRS(4326)),
                Proj(CRS(outmeta["crs"])), always_xy=True)
            geom = transform(project.transform, tile.geom)
            img, _ = rasterio.mask.mask(src, [geom], crop=True, nodata=255)
            return udm_analysis(tile, img)

    def download_udm(self, aoi, items, assettype, tmpdir, downloads={}):
        aoi.status("downloading {} assets".format(assettype))
        dl = downloader.create(self.client)
        def on_complete(self, item, asset, path=None):
            aoi.info("downloaded {} for {}".format(assettype, self["id"]))
            filename = asset
            asset = item["type"]
            downloads[self["id"]] = (asset, filename)
        dl.on_complete = on_complete
        dl.download(items, [assettype], tmpdir.name)
        return downloads

    def get_clouds(self, aoi, tiles, mosaic):
        scenes = [tile.item["id"] for tile in tiles]
        items = iter([tile.item for tile in tiles])
        if self.mask_bands:
            tmpdir = tempfile.TemporaryDirectory()
            downloads = self.download_udm(aoi, items, "ortho_udm2", tmpdir)
            missing_scenes = list(set(scenes) - set(downloads.keys()))
            # this used to be relevant but now PSScene always (?) has udm2
            #if missing_scenes:
            #    items = iter([tile.item for tile in tiles if tile.item["id"] in missing_scenes])
            #    self.downloads = self.download_udm(aoi, items, "udm", tmpdir, downloads)

        mosaic_clouds = 0
        mosaic_confidence = 0
        for tile in tiles:
            if self.mask_bands:
                try:
                    assettype = downloads[tile.id][0]
                    filename = downloads[tile.id][1]
                except:
                    raise Exception("no udm/udm2 was available for {}, trying another day".format(tile.id))
                if assettype == "ortho_udm2":
                    udm_analysis = self.udm2_analysis
                else:
                    udm_analysis = self.udm1_analysis
                clear, clouds, confidence = self.get_udm_clouds(filename, tile, udm_analysis)
                if clear + clouds == 0:
                    aoi.warn("udm has an error (does not fully overlap or classification problem)")
                    tile.clouds = 0
                    tile.confidence = 0
                else:
                    tile.clouds = 100 * float(clouds) / (clear + clouds)
                    tile.confidence = confidence
            else:
                tile.clouds = item_metadata_cloudiness(tile.item)
                tile.confidence = 0
            mosaic_clouds += tile.clouds * (tile.area / mosaic.area)
            mosaic_confidence += tile.confidence * (tile.area / mosaic.area)
            aoi.info("{} was {}% cloudy (confidence {}), overall clouds is now {}%".format(tile.id, int(tile.clouds), int(tile.confidence), int(mosaic_clouds)))
        return int(mosaic_clouds), int(mosaic_confidence)

    def get_tiles(self, aoi):
        geom_filter = api.filters.geom_filter(aoi.geom.convex_hull.__geo_interface__)
        for date in self.dates:
            try:
                aoi.status("checking {} for coverage".format(date.strftime("%Y-%m-%d"), self.min_cover))
                res = self.search_date(aoi, date, geom_filter)
                try:
                    tiles, mosaic, cover = self.build_mosaic(res, aoi)
                except ShapelyError as e:
                    aoi.error("geometry error: {}".format(e))
                    continue
                aoi.max_cover = max(aoi.max_cover, cover)
                if tiles:
                    aoi.info("found coverage of AOI on {} consisting of {} images and {}% coverage".format(date.strftime("%Y-%m-%d"), len(tiles), cover))
                    if self.max_clouds < 100:
                        clouds, confidence = self.get_clouds(aoi, tiles, mosaic)
                        aoi.min_clouds = min(aoi.min_clouds, clouds)
                        if clouds <= self.max_clouds and confidence >= self.min_confidence:
                            aoi.status("clouds on {} is {}%, ending search (confidence: {})".format(date.strftime("%Y-%m-%d"), clouds, confidence))
                            aoi.clouds = clouds
                            return date, tiles
                        else:
                            if clouds > self.max_clouds:
                                aoi.info("clouds on {} is {}%, continuing search".format(date.strftime("%Y-%m-%d"), clouds))
                            else:
                                aoi.info("clouds on {} is {}%, but confidence was only {}".format(date.strftime("%Y-%m-%d"), clouds, confidence))
                    else:
                        return date, tiles
            except Exception as e:
                aoi.error(e)
        aoi.info("no usable coverage found")
        return None, []

    def create_order(self, aoi, tiles, date):
        scenes = [tile.id for tile in tiles]
        tools = []
        tools.append({"clip": {"aoi": aoi.geom.convex_hull.__geo_interface__}})
        tools.append({"composite": {}})
        if self.crs:
            tools.append({"reproject": {"kernel": "cubic", "projection": self.crs}})
        filename = "{}_{}.zip".format(aoi.id, date.strftime("%d-%m-%Y"))
        products = [{"item_ids": scenes, "item_type": "PSScene", "product_bundle": self.bundle}]
        payload = {}
        payload["name"] = aoi.id
        payload["products"] = products
        payload["tools"] = tools
        payload["notifications"] = { "email": self.email }
        payload["delivery"] = {"archive_filename": filename, "archive_type": "zip"} # Planet client only works with zip
        res = self.client.create_order(payload)
        return res.get()["id"]

    def download_order(self, aoi, order_id):
        aoi.status("waiting for order {} to complete".format(order_id))
        def is_ready():
            res = self.client.get_individual_order(order_id).get()
            return res["state"] == "success"
        try:
            wait(is_ready, timeout_seconds=3600, expected_exceptions=APIException, sleep_seconds=((1,60)))
        except TimeoutExpired as e:
            aoi.warn("order {} took too long to complete, will not download".format(order_id))
            return None
        res = self.client.get_individual_order(order_id).get()["_links"]["results"]
        items = [item for item in res if "manifest.json" not in item["name"]]
        dl = downloader.create(self.client, order=True)
        #def on_complete(self, item, asset, path=None): # doesn't work for orders?
        #    files.append(asset)
        #dl.on_complete = on_complete
        locations = iter([item["location"] for item in items])
        dl.download(locations, [], ".")
        return [basename(item["name"]) for item in items] # just assume we got everything

    def __call__(self, aoi):
        try:
            aoi.status("searching for coverage")
            date, tiles = self.get_tiles(aoi)
        except Exception as e:
            aoi.warn("could not find suitable scenes")
            tiles = []

        order_id = None
        zip_file = None
        if tiles:
            if self.order:
                try:
                    order_id = self.create_order(aoi, tiles, date)
                    aoi.status("order created: {}".format(order_id))
                except Exception as e:
                    aoi.error("could not create order: {}".format(e))
            if order_id and self.download:
                try:
                    zip_file = self.download_order(aoi, order_id)
                    if zip_file:
                        aoi.status("downloaded {}".format(",".join(zip_file)))
                except Exception as e:
                    aoi.error("could not download order: {}".format(e))
        return (aoi, date, tiles, order_id, zip_file)
