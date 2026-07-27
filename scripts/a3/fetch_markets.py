import os, re, sys
import pandas as pd
from dotenv import load_dotenv
load_dotenv('/home/user/S4/.env')
from telonex import get_markets_dataframe
m = get_markets_dataframe(exchange="polymarket")
m.to_parquet('/home/user/S4/data/a3/telonex_markets_full.parquet')
m5 = m[m.slug.str.match(r'btc-updown-5m-\d+', na=False)].copy()
m5.to_parquet('/home/user/S4/data/a3/telonex_5m_markets.parquet')
m5['end'] = pd.to_datetime(m5.end_date_us, unit='us', utc=True)
print("rows", len(m), "5m", len(m5), "max end", m5['end'].max())
print(m5.sort_values('end').tail(5)[['slug','end','status','result_id','quotes_from','quotes_to','book_snapshot_5_from','book_snapshot_5_to']].to_string())
