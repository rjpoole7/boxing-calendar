import os
import sys
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from ics import Calendar, Event
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import time

# --- NEW: Import the advanced TLS spoofing library ---
from curl_cffi import requests

# 1. Setup & Auth
client = genai.Client()
FILE_NAME = "upcoming_fights.ics"

# 2. Fetch the LIVE links from the website
print("1. Fetching live schedule from website...")
url = "https://box.live/upcoming-fights-schedule/"

try:
    # Impersonate the exact cryptographic signature of Chrome
    response = requests.get(url, impersonate="chrome")
except Exception as e:
    print(f"❌ FATAL ERROR: Request failed entirely: {e}")
    sys.exit(1)

# --- THE FAILSAFE ---
if response.status_code != 200:
    print(f"❌ FATAL ERROR: The website blocked our connection (Status {response.status_code}).")
    print("Aborting script to protect existing calendar data.")
    sys.exit(1)

soup = BeautifulSoup(response.text, "html.parser")

live_fight_links = []
for a in soup.find_all("a", href=True):
    href = a["href"]
    if "-vs-" in href and "box.live" in href:
        if href.startswith("/"):
            href = f"https://box.live{href}"
        if href not in live_fight_links:
            live_fight_links.append(href)

# 3. Read the EXISTING calendar to find what we already know
existing_events = {}
if os.path.exists(FILE_NAME):
    print("2. Found existing calendar. Checking for known fights...")
    with open(FILE_NAME, "r", encoding="utf-8") as f:
        try:
            old_cal = Calendar(f.read())
            for event in old_cal.events:
                if event.url:
                    existing_events[event.url] = event
        except Exception as e:
            print(f"   Could not read old calendar cleanly, starting fresh. ({e})")
else:
    print("2. No existing calendar found. A new one will be created.")

# 4. Figure out exactly what is NEW
new_links = [link for link in live_fight_links if link not in existing_events]
print(f"-> Site has {len(live_fight_links)} total fights.")
print(f"-> We already have {len(existing_events)} saved.")
print(f"-> {len(new_links)} fights are brand new and need processing.")

# 5. Scrape ONLY the new links
new_scraped_pages = []
if new_links:
    print("\n3. Downloading data for NEW fights only...")
    for i, link in enumerate(new_links, 1):
        print(f"   [{i}/{len(new_links)}] Downloading: {link}")
        try:
            # Use the stealth impersonator for the deep links too
            page_resp = requests.get(link, impersonate="chrome")
            page_soup = BeautifulSoup(page_resp.text, "html.parser")
            page_text = page_soup.get_text(separator=" ", strip=True)
            new_scraped_pages.append({"link": link, "text": page_text})
            time.sleep(0.5)
        except Exception as e:
            print(f"   Error downloading {link}: {e}")

# 6. Process NEW links through Gemini
new_extracted_events = []
if new_scraped_pages:
    print("\n4. Processing new fights through Gemini...")

    prompt_template = """
    You are a sports data extractor. Look at the text extracted from multiple boxing event pages separated by markers.
    Extract the data and return it STRICTLY as a JSON array of objects.
    Each object must have exactly these keys:
    - "url": The exact URL provided in the 'START FIGHT PAGE' marker.
    - "main_event": The main fight (e.g., "Fighter A vs Fighter B").
    - "undercards": A list of strings for all undercard fights found.
    - "venue": The stadium/arena and city.
    - "broadcasters": A list of TV networks/streams.
    - "date": The date of the event in "YYYY-MM-DD" format.
    - "time": The start time in "HH:MM" (24-hour format). 
    - "timezone": The IANA timezone string (e.g., "UTC", "America/New_York"). Default to "UTC" if GMT or unknown.
    
    Website Text:
    """

    BATCH_SIZE = 15
    for idx in range(0, len(new_scraped_pages), BATCH_SIZE):
        batch = new_scraped_pages[idx:idx + BATCH_SIZE]
        print(f"   Analyzing batch {idx // BATCH_SIZE + 1} (Fights {idx + 1} to {idx + len(batch)})...")
        
        combined_text = ""
        for page in batch:
            combined_text += f"\n\n--- START FIGHT PAGE: {page['link']} ---\n{page['text']}\n"
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt_template + combined_text,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    ),
                )
                batch_events = json.loads(result.text)
                new_extracted_events.extend(batch_events)
                print(f"   ✅ Batch {idx // BATCH_SIZE + 1} successful!")
                break
                
            except Exception as api_err:
                print(f"   ❌ Batch failed on attempt {attempt + 1}: {api_err}")
                
                if attempt < max_retries - 1:
                    print("   ⏳ Waiting 60 seconds for Google's quota to reset before trying again...")
                    time.sleep(60)
                else:
                    print("   ⏭️ Skipping batch after maximum retries.")

        if idx + BATCH_SIZE < len(new_scraped_pages):
            print("   ⏳ Pausing 30 seconds before the next batch to respect rate limits...")
            time.sleep(30)

# 7. Build the Final Synced Calendar
print("\n5. Assembling the synced calendar...")
final_cal = Calendar()
aest_tz = ZoneInfo("Australia/Sydney")

processed_new_events = {item.get("url"): item for item in new_extracted_events if item.get("url")}

for link in live_fight_links:
    if link in existing_events:
        final_cal.events.add(existing_events[link])
        
    elif link in processed_new_events:
        item = processed_new_events[link]
        try:
            if not item.get("date") or item["date"] == "YYYY-MM-DD":
                continue
                
            e = Event()
            e.name = item["main_event"]
            e.location = item["venue"]
            e.url = link 
            
            tz_name = item.get("timezone", "UTC")
            if "GMT" in tz_name:
                tz_name = "UTC"
                
            try:
                event_tz = ZoneInfo(tz_name)
            except Exception:
                event_tz = ZoneInfo("UTC")
                
            dt_str = f"{item['date']} {item['time']}"
            local_start_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=event_tz)
            aest_start_dt = local_start_dt.astimezone(aest_tz)
            aest_end_dt = aest_start_dt + timedelta(hours=5, minutes=30)
            
            e.begin = aest_start_dt
            e.end = aest_end_dt
            
            networks = ", ".join(item["broadcasters"]) if item["broadcasters"] else "TBA"
            
            description = f"🔗 Fight Info: {link}\n"
            description += f"📺 Broadcasters: {networks}\n"
            description += f"📍 Venue: {item['venue']}\n"
            description += f"⏰ Start Time: {aest_start_dt.strftime('%A, %d %b at %I:%M %p')} AEST\n"
            description += "--------------------\n"
            description += "🥊 Undercard Fights:\n"
            
            if item.get("undercards"):
                for fight in item["undercards"]:
                    description += f"- {fight}\n"
            else:
                description += "- No undercards listed.\n"
                
            e.description = description
            final_cal.events.add(e)
            
        except Exception as err:
            print(f"   Error formatting new event {link}: {err}")

# 8. Save the state
with open(FILE_NAME, "w", encoding="utf-8") as f:
    f.writelines(final_cal.serialize_iter())

print(f"\nSuccess! '{FILE_NAME}' is completely synced with the live website.")
