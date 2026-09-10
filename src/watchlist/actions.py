"""Read-only corporate-action evidence through the official data SDK."""
from datetime import date
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest
from src.data.alpaca_calendar import finalized_day
from src.data.alpaca_config import load_config, PROJECT_ROOT
from src.data.alpaca_rate import SharedRateLimiter
from .runtime import root, write, read, utc, event


def download(universe):
    config=load_config()
    sdk=CorporateActionsClient(config.api_key,config.secret_key,raw_data=True)
    sdk._retry=2
    limiter=SharedRateLimiter(PROJECT_ROOT/'.runtime/alpaca-rate.sqlite')
    original=sdk._session.request
    def request(method,url,**kwargs):
        if method!='GET' or not url.startswith('https://data.alpaca.markets/'):
            raise ValueError('READ_ONLY_DATA_REQUIRED')
        limiter.acquire();kwargs['timeout']=(10,45)
        return original(method,url,**kwargs)
    sdk._session.request=request
    records=universe['records']+universe['references']
    statuses=[]
    for rec in records:
        path=root()/'actions'/(rec['symbol']+'.json')
        cached=read(path,{})
        if cached.get('end')==str(finalized_day()) and cached.get('status')=='ACCESS_OK':
            statuses.append({'symbol':rec['symbol'],'status':'ACCESS_OK'});continue
        try:
            data=sdk.get_corporate_actions(CorporateActionsRequest(symbols=[rec['symbol']],start=date.fromisoformat(rec['research_start']),end=finalized_day(),limit=None))
            result={'status':'ACCESS_OK','data':data,'source':'Alpaca corporate-actions data API',
                    'start':rec['research_start'],'end':str(finalized_day()),'retrieved_at':utc(),
                    'completeness':'NOT_INDEPENDENTLY_VERIFIED','dividend_payment_dates':'UNKNOWN_WHEN_ABSENT'}
        except Exception as exc:
            result={'status':'UNAVAILABLE','http_status':getattr(exc,'status_code',None),'retrieved_at':utc()}
        write(path,result);statuses.append({'symbol':rec['symbol'],'status':result['status']})
    write(root()/'actions_status.json',statuses)
    event('corporate-actions','COMPLETE',output=statuses)
    return statuses
