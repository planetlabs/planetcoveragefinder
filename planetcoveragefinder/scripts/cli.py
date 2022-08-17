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

from dateutil.rrule import rrule, DAILY
from pyproj import Proj, Transformer, CRS
from shapely import geometry
from shapely.ops import transform
import shutil
import click
import fiona
import json
import sys
from pathlib import Path
from planetcoveragefinder import Processor, AOI
from concurrent import futures
from planet import api
from requests import post
from tqdm import tqdm

MASK_BANDS = {
    "snow": 1,
    "shadow": 2,
    "lighthaze": 3,
    "heavyhaze": 4,
    "cloud": 5
}

def create_usable_features(aoi, tiles, order_id, zip_file):
    features = []
    for tile in tiles:
        properties = {"order": order_id, "aoi": aoi.id, "downloads": zip_file, "clouds": tile.clouds, "confidence": tile.confidence}
        features.append({"geometry": tile.geojson,
               "id": tile.id,
               "type": "Feature",
               "properties": properties})
    return features

def create_unusable_features(aois, attribute):
    features = []
    for aoi in aois:
        properties = {attribute: aoi.id, "max_cover": aoi.max_cover, "min_clouds": aoi.min_clouds}
        features.append({"geometry": aoi.geojson,
               "type": "Feature",
               "properties": properties})
    return features

def create_date_range(date1, date2):
    if not date2:
        dtstart = date1
        until = date1
    elif date1 > date2:
        dtstart = date2
        until = date1
    else:
        dtstart = date1
        until = date2
    dates = list(rrule(DAILY, dtstart=dtstart, until=until))
    dates.reverse()
    return dates

def get_mask_bands(mask_types):
    return [MASK_BANDS[t] for t in mask_types]

def get_features(filename, attribute, limit=None):
    features = []
    with fiona.open(filename) as source:
        project = Transformer.from_proj(
            Proj(CRS(source.crs["init"])),
            Proj(CRS("epsg:4326")), always_xy=True)
        digits = len(str(len(source)))
        for i,f in enumerate(source):
            if limit and limit <= i:
                break
            try:
                fid = f["properties"][attribute]
            except KeyError:
                fid = str(len(features)+1).zfill(digits)
            geom = transform(project.transform, geometry.shape(f["geometry"]))
            feature = (fid, geom, i)
            features.append(feature)
    return source.crs["init"], features

def create_tile_service(tiles):
    api_key = api.ClientV1().auth.value
    ids = [tile.id for tile in tiles]
    ids.reverse()
    data = {"ids" : ",".join(["{}:{}".format("PSScene", _id) for _id in ids])}
    res = post("https://tiles.planet.com/data/v1/layers", auth=(api_key, ""), data=data)
    if res.ok:
        name = res.json()["name"]
        wmts = "https://tiles.planet.com/data/v1/layers/wmts/{}?api_key={}".format(name, api_key)
        xyz = "https://tiles.planet.com/data/v1/layers/{}/{}".format(name, "{z}/{x}/{y}")
        return wmts, xyz
    else:
        return None, None

@click.command()
@click.argument("filename", type=click.Path(exists=True))
@click.argument("date1", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.argument("date2", type=click.DateTime(formats=["%Y-%m-%d"]), required=False)
@click.option("--order/--no-order", default=False, help="Submit an order for scenes.")
@click.option("--status/--no-status", default=False, help="Use a status bar for display.")
@click.option("-a", "--attribute", default=None, help="Attribute to uniquely identify features.")
@click.option("-b", "--bundle", default="analytic_sr_udm2,analytic_udm2", help="Bundles to download.")
@click.option("-c", "--max-clouds", default=100, type=click.IntRange(0, 100), help="Maximum cloud-cover percentage.")
@click.option("-C", "--confidence", default=0, type=click.IntRange(0, 100), help="Required confidence when using UDM-based cloudiness checks.")
@click.option("-m", "--mask-types", default=[], type=click.Choice(["cloud", "shadow", "lighthaze", "heavyhaze", "snow"]), multiple=True, show_default=True, help="UDM2 types to consider when evaluating cloudiness of images. If no types are specified, a cloudiness estimate based on the scene-level metadata will be used instead.")
@click.option("-d", "--download", is_flag=True, help="Download orders.")
@click.option("-e", "--email", is_flag=True, default=False, help="Send email for each order.")
@click.option("-f", "--frame", default=1, type=click.IntRange(min=1), help="Time frame in days to search for acceptable data (all images will be captured within this many days of one another).")
@click.option("-l", "--limit", default=0, type=click.INT, help="Maximum number of features to read from the input file.")
@click.option("-o", "--output", default=None, type=click.STRING, help="Name to use for all output files.")
@click.option("-p", "--thread-pool", default=1, type=click.IntRange(min=0), help="Number of AOIs to process simultaneously.")
@click.option("-q", "--image-quality", default="preferential", type=click.Choice(["all", "standard", "preferential"]), help="How to handle images of different quality.")
@click.option("-Q", "--qgis", is_flag=True, default=False, show_default=True, help="QGIS project file for results.")
@click.option("-r", "--reproject", default=None, type=click.INT, help="Reproject to a specific SRS (or no reprojection if set to 0).")
@click.option("-s", "--satellite", default=[], type=click.Choice(["PS0", "PS1", "PS2", "PS2.SD", "PSB.SD"]), multiple=True, help="Restrict results to specific satellite generations.")
@click.option("-t", "--min-cover", default=100, type=click.IntRange(1, 100), help="Required coverage percentage.")
@click.option("-u", "--geojson-unusable", is_flag=True, default=False, help="Save a GeoJSON with the AOIs for which there were not any usable results.")
@click.option("-x", "--xyz", is_flag=True, default=False, help="Create an XYZ tile service URL.")
@click.option("-w", "--wmts", is_flag=True, default=False, help="Create a WMTS tile service URL.")
@click.option("-v", "--verbosity", count=True, help="Verbosity (repeat for additional messages).")
def cli(filename, date1, date2, order, status, attribute, bundle, max_clouds, confidence, mask_types, 
        download, email, frame, geojson_unusable, image_quality, qgis, limit, output, thread_pool, 
        reproject, satellite, min_cover, xyz, wmts, verbosity):
    if not output:
        output = "{}_pcf".format(Path(filename).stem)
    if qgis:
        if not wmts:
            wmts = True # make sure we have a WMTS layer for our QGIS project
        qgis = "{}.qgz".format(output)
    geojson_usable = "{}.geojson".format(output)
    if geojson_unusable:
        geojson_unusable = "{}_failed.geojson".format(output)

    if mask_types:
        mask_bands = get_mask_bands(mask_types)
    else:
        mask_bands = None

    if download:
        order = True
    crs, features = get_features(filename, attribute, limit)

    if status and len(features) + 5 < shutil.get_terminal_size().lines:
        AOI.use_status = True
        pbar = tqdm(total=0, position=0, bar_format="{} AOIs {}".format("=","="*50))
        pbar.display()
        if verbosity:
            pbar.write("{} LOG {}".format("=","="*51))
    else:
        AOI.use_status = False
    AOI.verbosity = verbosity
    aois = [AOI(*feature) for feature in features]

    dates = create_date_range(date1, date2)
    if reproject == 0:
        crs = None
    elif reproject:
        crs = "epsg:{}".format(reproject)
    processor = Processor(dates, frame, max_clouds, confidence, min_cover, satellite, mask_bands, image_quality, order, email, crs, download, bundle)

    ex = futures.ThreadPoolExecutor(max_workers=thread_pool)
    wait_for = [
        ex.submit(processor, aoi)
        for aoi in aois
    ]

    features = []
    unusable = []
    output = []
    for f in futures.as_completed(wait_for):
        aoi, date, tiles, order_id, zip_file = f.result()
        if tiles:
            features += create_usable_features(aoi, tiles, order_id, zip_file)
            msg = ["SUCCESS (date: {} scenes: {})".format(date.strftime("%Y-%m-%d"), len(tiles))]
            if order_id:
                msg.append("Order ID {}".format(order_id))
                if zip_file:
                    msg.append("Downloaded {}".format(",".join(zip_file)))
            if wmts or xyz:
                wmts_url, xyz_url = create_tile_service(tiles)
                if wmts and wmts_url:
                    aoi.wmts_url = wmts_url
                    msg.append("WMTS: {}".format(wmts_url))
                if xyz and xyz_url:
                    aoi.xyz_url = xyz_url
                    msg.append("XYZ: {}".format(xyz_url))
            msg = ", ".join(msg)
        else:
            unusable.append(aoi)
            if aoi.max_cover < min_cover:
                if aoi.max_cover == 0:
                    msg = "FAILED (no coverage available)"
                else:
                    msg = "FAILED (coverage did not reach minimum threshold, maximum found was {}%)".format(aoi.max_cover)
            elif aoi.min_clouds > max_clouds:
                msg = "FAILED (clouds exceeded threshold, lowest found was {}%)".format(aoi.min_clouds)
            else:
                msg = "FAILED (found a coverage but confidence was too low)"
        aoi.status(msg)

    if AOI.use_status:
        pbar.close()

    if features:
        write_geojson(geojson_usable, features, max_clouds, min_cover)
    if geojson_unusable and unusable:
        unusable_features = create_unusable_features(unusable, attribute)
        write_geojson(geojson_unusable, unusable_features, max_clouds, min_cover)
    if qgis and features:
        write_qgis_project(qgis, aois, geojson_usable)

def write_geojson(filename, features, max_clouds, min_cover):
    layer = {"type": "FeatureCollection", "features": features,
            "_parameters": {"max_clouds": max_clouds, "min_cover": min_cover}}
    with open(filename, "w") as f:
        f.write(json.dumps(layer))

def write_qgis_project(filename, aois, geojson):
    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, QgsDataSourceUri, QgsApplication, QgsStyle, QgsCoordinateReferenceSystem
    from qgis.gui import QgsMapCanvas

    qgs = QgsApplication([], True)
    qgs.initQgis()
    p = QgsProject.instance()
    p.setFileName(filename)
    p.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    root = p.layerTreeRoot()

    get_styles = QgsStyle.defaultStyle()
    style = get_styles.symbol("outline red")

    for aoi in aois:
        quri = QgsDataSourceUri()
        quri.setParam("tileMatrixSet", "GoogleMapsCompatible23")
        quri.setParam("layers", "Combined scene layer")
        quri.setParam("format", "image/png")
        quri.setParam("crs", 'EPSG:3857')
        quri.setParam("styles", "")
        quri.setParam("url", aoi.wmts_url)
        rlayer = QgsRasterLayer(str(quri.encodedUri(), "utf-8"), "preview", "wms")
        p.addMapLayer(rlayer, False)

        vlayer = QgsVectorLayer(geojson, "footprints", "ogr")
        vlayer.setSubsetString("aoi='{}'".format(aoi.id))
        vlayer.renderer().setSymbol(style)
        p.addMapLayer(vlayer, False)

        group = root.addGroup(aoi.id)
        group.addLayer(vlayer)
        group.addLayer(rlayer)
        group.setItemVisibilityChecked(0)

    group.setItemVisibilityChecked(2)
    vlayer.updateExtents()
    canvas = QgsMapCanvas()
    canvas.setExtent(rlayer.extent())
    p.write()


if __name__ == "__main__":
    cli()
