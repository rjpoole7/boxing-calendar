import os
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from ics import Calendar, Event
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import time

# 1. Setup & Auth
# On GitHub Actions, the API key is passed directly via the environment
client = genai.Client()

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
FILE_NAME = "upcoming_fights.ics"


# ==========================================
# STEP 2: FIND ALL LIVE FIGHTS ON THE WEBSITE
# ==========================================
print("1. Fetching live schedule from website...")
url = "https://box.live/upcoming-fights-schedule/"
response = requests.get(url, headers=headers)
soup = BeautifulSoup(response.text, "html.parser")

live_fight_links = []
for a in soup.find_all("a", href=True):
    href = a["href"]
    if "-vs-" in href and "box.live" in href:
        if href.startswith("/"):
            href = f"https://box.live{href}"
        if href not in live_fight_links:
            live_fight_links.append(href)

# ==========================================
# STEP 3: READ YOUR EXISTING CALENDAR
# ==========================================
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

# ==========================================
# STEP 4: DECIDE WHICH FIGHTS NEED GEMINI
# ==========================================
# We only want to use Gemini on BRAND NEW fights, or fights happening 
# in the next 14 days (so we get updated undercards).
links_to_process = []
now_utc = datetime.now(timezone.utc)
UPDATE_WINDOW_DAYS = 14

for link in live_fight_links:
    if link not in existing_events:
        links_to_process.append(link) # It's a new fight
    else:
        try:
            event = existing_events[link]
            event_date = event.begin.datetime if hasattr(event.begin, 'datetime') else event.begin
            time_difference = event_date - now_utc
            
            # If the fight is happening soon, refresh it!
            if -1 <= time_difference.days <= UPDATE_WINDOW_DAYS:
                links_to_process.append(link)
        except Exception:
            pass

print(f"-> Site has {len(live_fight_links)} total fights listed.")
print(f"-> We already have {len(existing_events)} saved in your calendar.")
print(f"-> {len(links_to_process)} fights will be processed (new + upcoming refreshes).")

# ==========================================
# STEP 5: DOWNLOAD TEXT FOR THE REQUIRED FIGHTS
# ==========================================
new_scraped_pages = []
if links_to_process:
    print("\n3. Downloading data for required fights...")
    for i, link in enumerate(links_to_process, 1):
        print(f"   [{i}/{len(links_to_process)}] Downloading: {link}")
        try:
            page_resp = requests.get(link, headers=headers)
            page_soup = BeautifulSoup(page_resp.text, "html.parser")
            page_text = page_soup.get_text(separator=" ", strip=True)
            new_scraped_pages.append({"link": link, "text": page_text})
            time.sleep(0.2)
        except Exception as e:
            print(f"   Error downloading {link}: {e}")

# ==========================================
# STEP 6: USE GEMINI TO EXTRACT THE DATA
# ==========================================
new_extracted_events = []
if new_scraped_pages:
    print("\n4. Processing fights through Gemini...")

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
                # Using 2.5-flash to avoid strict rate limits
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

# ==========================================
# STEP 7: ASSEMBLE THE FINAL CALENDAR
# ==========================================
print("\n5. Assembling the synced calendar...")
final_cal = Calendar()
aest_tz = ZoneInfo("Australia/Sydney")

processed_new_events = {item.get("url"): item for item in new_extracted_events if item.get("url")}

# A. Add all live fights (using fresh data if we have it, or old data if we don't)
for link in live_fight_links:
    if link in processed_new_events:
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
            print(f"   Error formatting event {link}: {err}")
            
    elif link in existing_events:
        final_cal.events.add(existing_events[link])

# B. Keep historical events (fights that happened and were removed from the site)
for link, old_event in existing_events.items():
    if link not in live_fight_links:
        final_cal.events.add(old_event)

# ==========================================
# STEP 8: SAVE TO FILE
# ==========================================
with open(FILE_NAME, "w", encoding="utf-8") as f:
    f.writelines(final_cal.serialize_iter())

print(f"\nSuccess! '{FILE_NAME}' is completely synced and updated with the live website.")
