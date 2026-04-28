"""Refresh the local dashboard data files from Railway PostgreSQL.

server.py runs this script every 30 seconds. The script reads new rows from the
current day's PostgreSQL partition, appends them to data_YYYY-MM-DD.xlsx as a raw
local cache, calculates the dashboard metrics, and writes gui_YYYY-MM-DD.xlsx for
popsicle_dashboard.html to load.
"""

import pandas as pd
from sqlalchemy import create_engine
import os
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")
pd.options.mode.chained_assignment = None


# --- CONFIGURATION ---
# Public Railway PostgreSQL connection string used by the local dashboard.
DB_URL = "postgresql://postgres:QbcDZnDSLdphWUiKWtMnpTuoZgYvGUpm@monorail.proxy.rlwy.net:10094/railway"

now = datetime.now()

db_name = f"events_p_{now:%Y%m%d}"
current_date = now.strftime('%Y-%m-%d')
filename = f"data_{current_date}.xlsx"
guifilename = f"gui_{current_date}.xlsx"



def refresh_data():
    """Pull new PostgreSQL rows, update local caches, and write dashboard metrics."""

    # Load the current day's raw cache. The highest cached id lets the SQL query
    # request only rows that arrived after the previous refresh.
    try:
        data = pd.read_excel(filename, index_col = 0)
        last_id = data['id'].max()
        if pd.isna(last_id):
            last_id = 0
    except:
        data = pd.DataFrame()
        last_id = 0

    # Pull new rows from the current day's PostgreSQL partition.
    try:
        engine = create_engine(DB_URL)
    
        SQL_QUERY = f"SELECT * FROM {db_name} WHERE id > %(last_id)s;"

        query_params = {'last_id': last_id}

        df = pd.read_sql(SQL_QUERY, engine, params=query_params)
        
    except:
        df = pd.DataFrame()
        print('Database query failed; using cached data only')
    print(len(df), len(data))
    

    df = pd.concat([data, df], ignore_index=True)
    try:
        df['timestamp'] = pd.to_datetime(df['timestamp'])
    except:
        print('No timestamp column found, might be an empty df? This will error out in a second.')
    try:
        df = df.sort_values('id')
    except:
        df = df

    try:
        # Split the raw event table by sensor name. Add new dashboard sensors here
        # when they should contribute to metrics or display values.
        wrap_df = df[df['sensor_name'] == 'Dry Contact - Wrap']
        air_df = df[df['sensor_name'] == 'Dry Contact - Air Cooled']
        water_df = df[df['sensor_name'] == 'Dry Contact - Water Cooled']
        freezer_container_df = df[df['sensor_name'] == 'Freezer Container']
        freezer_main_df = df[df['sensor_name'] == 'Freezer Main']
        fridge_blend_df = df[df['sensor_name'] == 'Fridge Blend']
        fridge_hall_df = df[df['sensor_name'] == 'Fridge Hall']
    except:
        print('No sensor_name column found, might be pulling from an empty df')






    # Wrap count: one closed-to-open transition within 61 seconds counts as one
    # wrap cycle, and the current production assumption is 2 pops per cycle.
    try:
        time_diff = abs(wrap_df['timestamp'].diff())

        mask = (
                (wrap_df['state'] == 'Open') & 
                (wrap_df['state'].shift(1) == 'Closed') & 
                (time_diff <= pd.Timedelta(seconds=61))
            )
        wrap_df = wrap_df[mask]
    except:
        wrap_df = pd.DataFrame()
        print('wrap_df dataframe errored on creation ---- replacing with an empty df')

    current_wrapped = len(wrap_df) * 2



    

    # Air-cooled pull events and the 30 most recent air pull ages.
    mask = (
            (air_df['state'] == 'Open') & 
            (air_df['state'].shift(1) == 'Closed')
        )
    air_df = air_df[mask]
    if len(air_df) < 30:
        recent_air = air_df
        now = pd.Timestamp.now()
        recent_air['time_diff'] = now - recent_air['timestamp']
        recent_air = recent_air['time_diff']
    else:
        recent_air = air_df.tail(30)
        now = pd.Timestamp.now()
        recent_air['time_diff'] = now - recent_air['timestamp']
        recent_air = recent_air['time_diff']


    try:
        air_minutes = [td.total_seconds() / 60 for td in list(recent_air)]

        l_w = len(air_minutes)
        if l_w < 30:
            new = [0 for i in range (30-l_w)]
            air_minutes = new + air_minutes
    except:
        air_minutes = [0 for i in range(30)]


    # Water-cooled pull events and the 30 most recent water pull ages.
    mask = (
            (water_df['state'] == 'Open') & 
            (water_df['state'].shift(1) == 'Closed')
        )
    water_df = water_df[mask]
    #print(water_df)

    if len(water_df) < 30:
        recent_water = water_df
        now = pd.Timestamp.now()
        recent_water['time_diff'] = now - recent_water['timestamp']
        recent_water = recent_water['time_diff']
    else:
        recent_water = water_df.tail(30)
        now = pd.Timestamp.now()
        recent_water['time_diff'] = now - recent_water['timestamp']
        recent_water = recent_water['time_diff']

    try:
        water_minutes = [td.total_seconds() / 60 for td in list(recent_water)]

        l_w = len(water_minutes)
        if l_w < 30:
            new = [0 for i in range (30-l_w)]
            water_minutes = new + water_minutes
    except:
        water_minutes = [0 for i in range(30)]

    # Pull totals: each air or water pull currently represents 28 popsicles.
    total_air_pulled = 28 * len(air_df)
    total_water_pulled = 28 * len(water_df)
    total_pull = total_air_pulled + total_water_pulled


    # Shift-average rates are calculated from the first event of the day through now.
    timenow = pd.Timestamp.now()

    try:
        first_water_pull = water_df['timestamp'].iloc[0]
    except:
        first_water_pull = timenow
    try:
        first_air_pull = air_df['timestamp'].iloc[0]
    except:
        first_air_pull = timenow

    try:
        first_wrap = wrap_df['timestamp'].iloc[0]
    except:
        first_wrap = timenow
    #print(first_air_pull, first_water_pull)

    # Water pull rate.
    try:
        water_rate = len(water_df)/(timenow - first_water_pull).total_seconds() * 60
    except:
        water_rate = 0

    # Air pull rate.
    try:
        air_rate = len(air_df)/(timenow - first_air_pull).total_seconds() * 60 
        air_avg_min = 1 / air_rate
    except:
        air_rate = 0

    # Wrap rate.
    try:
        wrap_rate = len(wrap_df) / (timenow - first_wrap).total_seconds() * 60 
        print(len(wrap_df))
        print(wrap_df)
    except:
        wrap_rate = 0


    # Short-term pull rates based on the last 10 pull events.
    try:
        last_10_pull_air = air_df.tail(10)
        dif = air_df['timestamp'].iloc[0] - timenow
        last_10_air_rate = len(last_10_pull_air) / abs(dif.total_seconds()) * 60

    except:
        last_10_air_rate = 0


    try:
        last_10_pull_water = water_df.tail(10)
        dif = water_df['timestamp'].iloc[0] - timenow
        last_10_water_rate = len(last_10_pull_water) / abs(dif.total_seconds()) * 60
    except:
        last_10_water_rate = 0


    # Rolling wrap rate over the last 30 minutes.
    try:
        last_30_min_wrap = wrap_df[np.where(((timenow - wrap_df['timestamp']).total_seconds() * 60)<= 30, True, False)]
        last_30_wrap_rate = len(last_30_min_wrap) / 30
    except:
        last_30_wrap_rate = 0


    # Temperature tracking: keep the most recent value from each configured
    # freezer/fridge sensor and write it into gui_YYYY-MM-DD.xlsx.
    freezer_container_df = df[df['sensor_name'] == 'Freezer Container']
    freezer_main_df = df[df['sensor_name'] == 'Freezer Main']
    fridge_blend_df = df[df['sensor_name'] == 'Fridge Blend']
    fridge_hall_df = df[df['sensor_name'] == 'Fridge Hall']
    try:
        l_5_fc = freezer_container_df['state'].tail(1).tolist()
    except:
        l_5_fc = []

    #Freezer Main
    try:
        l_5_fm = freezer_main_df['state'].tail(1).tolist()
    except:
        l_5_fm = []

    #Fridge Blend
    try:
        l_5_fb = fridge_blend_df['state'].tail(1).tolist()
    except:
        l_5_fb = []

    #Fridge Hall
    try:
        l_5_fh = fridge_hall_df['state'].tail(1).tolist()
    except:
        l_5_fh = []


    # Persist the raw cache first, then write the compact workbook consumed by
    # popsicle_dashboard.html. If output keys change here, update the HTML parser.
    df.to_excel(filename)


    outputs = {'Total_Pulled' : total_pull, 'Total_Air_Pulled' : total_air_pulled, 
    'Total_Water_Pulled' : total_water_pulled, 'Total Wrapped' : current_wrapped,
    '30_min_wrap_rate' : last_30_wrap_rate, 'L10_air_rate' : last_10_air_rate,
    'L10_water_rate' : last_10_water_rate, 'total_wrap_rate' : wrap_rate,
    'total_air_rate' : air_rate, 'total_water_rate' : water_rate,
    '30_air_time' : air_minutes, '30_water_time' : water_minutes,
    'freezer_container' : l_5_fc, 'freezer_main' : l_5_fm,
    'fridge_blend' : l_5_fb, 'fridge_hall' : l_5_fh}

    outputs = pd.DataFrame.from_dict(outputs, orient='index')

    outputs.to_excel(guifilename)




if __name__ == "__main__":
    refresh_data()
