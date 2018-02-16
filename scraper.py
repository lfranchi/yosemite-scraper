import json
from html.parser import HTMLParser

import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from collections import defaultdict
from secrets import *
from templates import *
from twilio.rest import TwilioRestClient

BASE_URL = 'http://www.recreation.gov'
CAMP_REQUEST_URL = BASE_URL + '/campsiteCalendar.do'
PERMIT_REQUEST_URL = BASE_URL + "/permits/{entrance_name}/r/entranceDetails.do"
MG_URL = 'https://api.mailgun.net/v3/{}/messages'.format(MG_DOMAIN)
INLINER_URL = 'https://inlinestyler.torchbox.com/styler/convert/'
client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

config = None
with open('config.json') as config_file:
    config = json.loads(config_file.read())

def extract_camps_from_page(page_soup):
    """Follow all 'next' links"""

    camp_name = camp_soup.find(id='cgroundName').string
    calendar_body = soup.select('#calendar tbody')[0]
    camps = calendar_body.find_all('tr', attrs={'class': None})


def find_campsite(campsite_request):
    found = 0
    start_date = datetime.strptime(campsite_request['start_date'], '%m/%d/%Y')
    # Only match the exact number of days requested
    days = [start_date + timedelta(days=campsite_request['length'])]
    day_strs = [day.strftime('%m/%d/%Y') for day in days]

    avail_camps = dict((day_str, defaultdict(list)) for day_str in day_strs)
    unavail_camps = dict((day_str, defaultdict(list)) for day_str in day_strs)

    for park_id in campsite_request['park_ids']:
        # print "Requesting campsite for park {} on {} for {} days".format(
        #    park_id, campsite_request['start_date'], campsite_request['length'])
        payload = {
            'page': 'matrix',
            'contractCode': 'NRSO',
            'calarvdate': campsite_request['start_date'],
            'parkId': park_id
        }
        site_filter = campsite_request.get('sitefilter')
        if site_filter:
            payload['sitefilter'] = site_filter
        payload_str = "&".join(
            "%s=%s" % (k, v) for k, v in list(payload.items()))
        response = requests.get(CAMP_REQUEST_URL, params=payload_str)
        print("URL is: " + response.url)
        if not response.ok:
            print("Request failed for park {} on {}".format(park_id,
                                                            campsite_request[
                                                                'start_date']))
            continue

        soup = BeautifulSoup(response.text, 'html.parser')
        camp_name = soup.find(id='cgroundName').string
        calendar_body = soup.select('#calendar tbody')[0]
        camps = calendar_body.find_all('tr', attrs={'class': None})

        for camp in camps:
            site_number_tag = camp.select('.siteListLabel a')[0]
            site_number = site_number_tag.string
            site_url = site_number_tag['href']
            if site_number.startswith('HRS'):  # horse campsite
                continue
            elif site_number.startswith('RV'):  # RV campsite
                continue
            elif 'BOAT-IN' in site_number:
                continue

            status_tags = camp.select('.status')
            for day_str, status_tag in zip(day_strs, status_tags):
                # print("Checking for: " + day_str + ": " + status_tag.string)
                if status_tag.string in ('R', 'X'):  # reserved, unavailable
                    unavail_camps[day_str][camp_name].append(site_number)
                elif status_tag.string in (
                'w', 'W', 'n', 'N'):  # Walk-in or closed for the season
                    unavail_camps[day_str][camp_name].append(site_number)
                elif status_tag.string == 'C':
                    avail_camps[day_str][camp_name].append((site_number,
                                                            site_url,
                                                            'call'))
                    found += 1

                else:
                    if not status_tag.find('a'):
                        print(
                            "No <a> tag for this camp: %s on %s with site num %s and url %s." % (
                            camp_name, day_str, site_number, site_url))
                        print("Status tag string is %s" % (status_tag.string,))
                        unavail_camps[day_str][camp_name].append(site_number)
                    else:
                        reservation_url = BASE_URL + status_tag.find('a')[
                            'href']
                        avail_camps[day_str][camp_name].append((site_number,
                                                                reservation_url,
                                                                'reserve'))
                        found += 1

    if len(list(avail_camps.items())) == 0:
        print("Nothing found...")
        return None, None
    from pprint import pprint as pp
    pp(avail_camps)
    # filter out empty dates
    # print "Starting with %s" % (campsite_request['start_date'],)
    total_avail = sum(
        map(len, [list(c.values()) for c in list(avail_camps.values())]))
    # print "Total avail: %s" % total_avail
    if total_avail == 0:
        return None, None

    body = TRIP.format(campsite_request['start_date'])
    for day_str, camps in iter(sorted(avail_camps.items())):
        print("Found %s, %s" % (day_str, camps))
        if not camps:
            return None, None
        camps_html = '' if camps else 'None'
        for camp_name, sites in camps.items():
            sites_html = '' if sites else 'None'
            for site_number, url, action in sites:
                sites_html += SITE.format(site_number, url, action)
            camps_html += CAMP.format(camp_name, sites_html)
        body += DAY.format(day_str, camps_html)

    return found, body

def send_campsite_notifications(num_found, body):
    with open('style.min.css') as css_file:
        html = HTML.format(css_file.read(), body)

    response = requests.post(INLINER_URL, data={
        'returnraw': 'y',
        'source': html
    })
    h = HTMLParser()
    inlined_html = h.unescape(response.text)

    # Text me as well
    # extract url
    soup = BeautifulSoup(html, 'html.parser')
    url = soup.find('a')['href']
    tmp_file = open('/tmp/yosemite_scraper_urls.log', 'a')
    tmp_file.write(url + '\n')
    tmp_file.close()
    
    # client.messages.create(
    #    to=TARGET_PHONE,
    #    from_=TWILIO_SOURCE_PHONE,
    #    body="Found Yosemite Campsites, check your email!"
    # )

    requests.post(MG_URL, auth=('api', MG_KEY), data={
        'from': '"Yosemite Campsite Scraper" <yosemite@lfranchi.com>',
        'to': ','.join(config['emails']),
        'subject': 'Found {} camp sites near Yosemite'.format(num_found),
        'html': inlined_html
    })


def find_inyo_permits(permit_request):
    entry_date = datetime.strptime(permit_request['start_date'], '%m/%d/%Y')
    entry_date_formatted = entry_date.strftime('%m/%d/%Y')
    trailhead_entrance_id = int(permit_request['trailhead_entrance_id'])
    permit_type_id = int(permit_request['permit_type_id'])
    group_size = int(permit_request['group_size'])

    park_id = 72203 # Inyo national forest

    # this part of the path seems to be unused: it's usually the name of the
    # entrance (spaces replaced with _) but doesn't seem to matter if it's different
    # so we ignore it
    request_url = PERMIT_REQUEST_URL.format(entrance_name="")
    payload = {
        'parkId': park_id,
        'entranceId': trailhead_entrance_id,
        'pGroupSize': group_size,
        'permitTypeId': permit_type_id,
        'arvdate': entry_date_formatted,
        'contractCode': 'NRSO',
    }
    payload_str = "&".join(
        "%s=%s" % (k, v) for k, v in list(payload.items()))
    response = requests.get(request_url, params=payload_str)
    print(f"Requesting permit with URL: {response.url}")

    if not response.ok:
        print("Request failed for permit {} on {}\n\nURL was: {}".format(
            trailhead_entrance_id, entry_date_formatted, response.url))
        return

    # Parse response page :)
    soup = BeautifulSoup(response.text, 'html.parser')
    permit_grid = soup.find(id="permitGridContainer")

    # First grid item is the desired day
    first_row = permit_grid.find('tbody').find('td')

    # If there's a link with "A" in it, it's available
    avail_link = first_row.find("a")
    permit_available = avail_link and 'A' in avail_link.text.upper()
    if permit_available:
        print("Available permit!")
    else:
        print("All permits reserved :-/")

    return permit_available, response.url

def send_permit_notifications(found_url, to_emails):
    # Text me as well
    # client.messages.create(
    #     to=TARGET_PHONE,
    #     from_=TWILIO_SOURCE_PHONE,
    #     body="Found Inyo Permit: check your email or click here: %s" % (found_url,)
    # )

    requests.post(MG_URL, auth=('api', MG_KEY), data={
        'from': '"Yosemite Campsite Scraper" <yosemite@lfranchi.com>',
        'to': ','.join(to_emails),
        'subject': 'Found permits for Inyo National Forest!',
        'text': "Found Inyo Permit: check {}".format(found_url)
    })


for trip_request in config['trips']:
    request_type = trip_request.get("type", "campsite")
    if request_type == "campsite":
        num_found, found_campsites = find_campsite(trip_request)
        if found_campsites:
            send_campsite_notifications(num_found, found_campsites)
    elif request_type == "inyo_permit":
        found, url = find_inyo_permits(trip_request)
        if found:
            send_permit_notifications(url, config['emails'])
