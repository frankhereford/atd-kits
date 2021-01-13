#!/usr/bin/env python
""" Fetch traffic signal statuses from traffic management system (KITS) and publish
to Open Data Portal.

It works like this:
1. Query KITS MSSQL DB for signals in with status 1, 2, or 3. These indicate flashing
signals or communication outages.
2. Fetch the stale signal status data published on the Open Data Portal
3. Compare the KITS signal status data against the stale status data from Socrata, and
delete/upsert the current data

This script should be scheduled at 5 min intervals and is consumed by the Traffic Signal
Monitor dashboard, available here: https://data.mobility.austin.gov/signals-on-flash/
"""
import os

import arrow
import pymssql
import requests
import sodapy

import utils

# docker run --network host -it --rm --env-file /Users/john/Dropbox/atd/atd-kits/env_file -v /Users/john/Dropbox/atd/atd-kits:/app/atd-kits atddocker/atd-kits /bin/bash
KITS_SERVER = os.getenv("KITS_SERVER")
KITS_USER = os.getenv("KITS_USER")
KITS_PASSWORD = os.getenv("KITS_PASSWORD")
KITS_DATABSE = os.getenv("KITS_DATABSE")
SOCRATA_APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN")
SOCRATA_API_KEY_ID = os.getenv("SOCRATA_API_KEY_ID")
SOCRATA_API_KEY_SECRET = os.getenv("SOCRATA_API_KEY_SECRET")

FLASH_STATUSES = [1, 2, 3]
SIGNAL_STATUS_RESOURCE_ID = "5zpr-dehc"
SIGNALS_RESOURCE_ID = "p53x-x73x"


def get_kits_signal_status(server, user, password, database):
    """ Fetch traffic signal operation statuses from the KITS mssql database """
    query = f"""
        SELECT
            status.DATETIME as operation_state_datetime,
            status.STATUS as operation_state,
            status.PLANID as plan_id,
            signal.ASSETNUM as signal_id
        FROM [KITS].[INTERSECTION] signal
        LEFT OUTER JOIN [KITS].[INTERSECTIONSTATUS] status
        ON signal.[INTID] = status.[INTID]
        WHERE
            status.DATETIME IS NOT NULL
        AND
            status.STATUS in ({",".join([str(s) for s in FLASH_STATUSES])})
        ORDER BY status.DATETIME DESC
    """
    with pymssql.connect(
        server=server, user=user, password=password, database=database, timeout=10,
    ) as conn:
        with conn.cursor(as_dict=True) as cursor:
            cursor.execute(query)
            return cursor.fetchall()


def get_socrata_data(resource_id, params):
    endpoint = f"https://data.austintexas.gov/resource/{resource_id}.json"
    res = requests.get(endpoint, params=params)
    res.raise_for_status()
    return res.json()


def stringify_signal_ids(kits_sig_status, key="signal_id"):
    for sig in kits_sig_status:
        sig[key] = str(sig[key])
    return


def identify_deletes(kits_sig_status, socrata_sig_status):
    """ Identify signals that need to be deleted from socrata signal status dataset and
    return the delete payload"""
    delete_signals = []
    kits_ids = [s["signal_id"] for s in kits_sig_status]
    socrata_ids = [s["signal_id"] for s in socrata_sig_status]
    for signal_id in socrata_ids:
        if signal_id not in kits_ids:
            delete_signals.append({"signal_id": signal_id, ":deleted": True})
    return delete_signals


def merge_signal_asset_data(kits_sig_status, signal_asset_data):
    """Update kits signal status data with asset info"""
    asset_fields = ["location", "location_name", "primary_st", "cross_st"]

    for kits_signal in kits_sig_status:
        matched_signal_list = [
            a for a in signal_asset_data if a["signal_id"] == kits_signal["signal_id"]
        ]
        if not matched_signal_list:
            # kits may have test/lab signals that are not known to our asset tracking
            # ignore these
            continue
        # assume there's only one. if there are multiple, we probably don't want to
        # stop this script. likely an issue with dupes in the signal assets ETL
        matched_signal = matched_signal_list[0]
        kits_signal.update({key: matched_signal.get(key) for key in asset_fields})
    return


def localize_operation_state_datetime(
    kits_sig_status, key="operation_state_datetime", tz="US/Central"
):
    for signal in kits_sig_status:
        dt = arrow.get(signal[key])
        # kits timestamps are in US/Central
        dt_local = dt.replace(tzinfo=tz)
        # socrata-friendly format (no TZ info)
        signal[key] = dt_local.format("YYYY-MM-DDTHH:mm:ss")
    return


def set_processed_datetime(kits_sig_status, tz="US/Central"):
    for signal in kits_sig_status:
        now = arrow.now(tz)
        signal["processed_datetime"] = now.format("YYYY-MM-DDTHH:mm:ss")
    return


def convert_decimals(kits_sig_status, keys=["operation_state", "plan_id"]):
    for signal in kits_sig_status:
        for key in keys:
            signal[key] = int(signal[key])
    return


def format_locations(kits_sig_status):
    for signal in kits_sig_status:
        lat = signal.get("location", {}).get("latitude")
        lon = signal.get("location", {}).get("longitude")
        if lat and lon:
            signal["location"] = f"({lon}, {lat})"
    return


def main():
    kits_sig_status = get_kits_signal_status(
        KITS_SERVER, KITS_USER, KITS_PASSWORD, KITS_DATABSE
    )

    logger.info(f"{len(kits_sig_status)} records to process.")

    stringify_signal_ids(kits_sig_status)

    # get the current signal statuses from socrata (these are the records we will
    # update, replace, or delete
    socrata_sig_status = get_socrata_data(SIGNAL_STATUS_RESOURCE_ID, {"$limit": 99999})

    delete_signals = identify_deletes(kits_sig_status, socrata_sig_status)

    logger.info(f"{len(delete_signals)} to be deleted.")

    fetch_signal_ids = [signal["signal_id"] for signal in kits_sig_status]

    params = {
        "$where": f"signal_id in ({','.join(fetch_signal_ids)})",
        "$limit": 99999,
    }
    # get asset data about each signal (street names, location, etc)
    signal_asset_data = get_socrata_data(SIGNALS_RESOURCE_ID, params)

    merge_signal_asset_data(kits_sig_status, signal_asset_data)

    localize_operation_state_datetime(kits_sig_status)

    set_processed_datetime(kits_sig_status)

    convert_decimals(kits_sig_status)

    format_locations(kits_sig_status)

    client = sodapy.Socrata(
        "data.austintexas.gov",
        SOCRATA_APP_TOKEN,
        username=SOCRATA_API_KEY_ID,
        password=SOCRATA_API_KEY_SECRET,
    )

    client.upsert(SIGNAL_STATUS_RESOURCE_ID, kits_sig_status + delete_signals)


if __name__ == "__main__":
    logger = utils.logging.getLogger(__file__)
    main()