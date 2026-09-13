"""Ben-only current business policy, retaining all 66 source identities."""
import json
import pandas as pd
from .freeze import DATA, ROOT, now, write

# Primary sources actually read in this run. They support current business,
# not the claim that this business classification was known in 2016.
REVIEWED = {
    'SM': ('EXCLUDE_OIL_GAS','Independent oil and gas exploration and production','https://www.sm-energy.com/'),
    'BATL': ('EXCLUDE_OIL_GAS','Oil and gas acquisition, production and exploration in Delaware Basin','https://battalionoil.com/'),
    'AVAV': ('EXCLUDE_DEFENSE_PRIMARY','Integrated defense systems, military unmanned aircraft and precision strike','https://www.avinc.com/about/'),
    'BE': ('KEEP','Onsite fuel cell power technology; not direct oil/gas production','https://www.bloomenergy.com/'),
    'FCEL': ('KEEP','Fuel cell energy technology platform','https://www.fuelcellenergy.com/'),
    'PLTR': ('KEEP','Government and commercial software platforms; government customers do not alone establish weapons manufacturing','https://www.palantir.com/docs/foundry/platform-overview/overview//'),
    'RKLB': ('KEEP','Launch services, spacecraft and satellite components for government and commercial operators','https://rocketlabcorp.com/'),
    'AMZN': ('KEEP','Retail, AWS, devices and services','https://www.aboutamazon.com/what-we-do'),
    'AAOI': ('KEEP','Optical communications products for cable, data centers and telecom','https://ao-inc.com/about-us/'),
    'WULF': ('KEEP','Digital infrastructure / HPC / bitcoin mining history, not direct oil/gas production','https://www.terawulf.com/about'),
    'OKLO': ('KEEP','Advanced fission power, nuclear fuel recycling and isotope production','https://oklo.com/'),
    'ECHO': ('KEEP','Satellite, wireless, broadband and television communications','https://www.echostar.com/'),
    'SIDU': ('UNKNOWN','Space and defense technology/hardware and dual-use data; primary defense business share not established','https://sidusspace.com/'),
    'INFQ': ('KEEP','Quantum computing, sensing, neutral atom products and photonics; no evidence of primary weapons manufacturing','https://infleqtion.com/'),
    'QNT': ('KEEP','Quantum computing technology and software','https://www.quantinuum.com/'),
    'MRNA': ('KEEP','mRNA medicines and vaccines','https://www.modernatx.com/'),
    'USAR': ('KEEP','Rare earth mining, processing and magnet manufacturing; not oil/gas extraction','https://www.usare.com/'),
    'SPCX': ('KEEP','Launch vehicles, spacecraft and connectivity infrastructure','https://new.spacex.com/mission'),
    'VELO': ('KEEP','Metal additive manufacturing equipment, software and production services','https://www.velo3d.com/'),
    'XE': ('KEEP','Advanced nuclear reactor and fuel technology','https://x-energy.com/'),
}


def run():
    universe=json.loads((DATA/'universe.json').read_text(encoding='utf-8'))
    rows=[]
    additions_path=ROOT/'scope_additions.json'
    additions={x['symbol']:x for x in json.loads(additions_path.read_text(encoding='utf-8'))} if additions_path.exists() else {}
    for rec in universe['records']:
        symbol=rec['symbol']
        if symbol in REVIEWED:
            status,business,source=REVIEWED[symbol]
            basis='CURRENT_ISSUER_PRIMARY_SOURCE_REVIEW'
        elif rec['type'] in ['CEF','ETF_OR_EXCLUDED_SECURITY','SPAC_COMMON']:
            status='EXCLUDE_NON_OPERATING_SECURITY'
            business=rec['type']; source=';'.join(rec.get('sources',[])); basis='EXISTING_IDENTITY_EVIDENCE'
        elif symbol in additions:
            item=additions[symbol]
            status=item['scope_policy'];business=item['business'];source=item['business_source'];basis=item['evidence_basis']
        else:
            status='UNKNOWN'
            business='PRIMARY_BUSINESS_REVIEW_INCOMPLETE'
            source=';'.join(rec.get('sources',[])); basis='IDENTITY_EVIDENCE_ONLY_DOES_NOT_VERIFY_BUSINESS'
        rows.append({'symbol':symbol,'name':rec['name'],'type':rec['type'],
                     'stable_local_security_id':'LOCAL_'+rec['identity_version'],
                     'global_security_id':rec.get('security_id','UNKNOWN'),'identity_version':rec['identity_version'],
                     'identity_status':rec['status'],'historical_mapping':rec.get('historical_mapping','UNKNOWN'),
                     'research_start_boundary':rec['research_start'],'scope_policy':status,'business':business,
                     'business_source':source,'source_checked_at':now(),'evidence_basis':basis,
                     'historical_scope':'CURRENT_SCOPE_APPLIED_TO_HISTORICAL_EXPLORATION',
                     'candidate_retained':True,'strict_scope_eligible':status=='KEEP',
                     'old_M20_U_universe_changed':False})
    assert len(rows)==66
    pd.DataFrame(rows).to_csv(ROOT/'UNIVERSE_POLICY.csv',index=False,encoding='utf-8-sig')
    write(ROOT/'scope_receipt.json',{'at':now(),'rows':len(rows),'statuses':pd.Series([x['scope_policy'] for x in rows]).value_counts().to_dict(),
                                    'unreviewed_remain_in_coarse_scan':True,'unknown_is_not_a_new_blacklist':True})


if __name__=='__main__':run()
