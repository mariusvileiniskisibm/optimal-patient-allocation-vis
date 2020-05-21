from flask import Flask, render_template, request, jsonify, url_for
import atexit
import os
import json
import folium
from botocore.client import Config
import ibm_boto3
import pandas as pd
import ast
from collections import namedtuple
import numpy as np

class ProvinceMap:
    def __init__(self, province_mapping):
        self._mp = pd.read_csv('province_id_mapping.csv')
        
    def get_id(self, province):
        return self._mp.loc[self._mp.province_nl.str.contains(province) | self._mp.province_en.str.contains(province), "id"].values[0]
    
    def get_prov(self, p_id):
        return self._mp.loc[self._mp.id==p_id, "province_en"].values[0]
    
    @property
    def index_list(self):
        return list(self._mp.id)
    
province_map = ProvinceMap("province_id_mapping.csv")


def get_provinces_routes_dur(file_name):
    # reading province_coordinates.csv to coordinates_dict
    body = read_input_file(file_name)
    coordinates_df = pd.read_csv(body)
    coordinates_dict = coordinates_df.set_index('Province Name').T.to_dict('list')
    
    url = 'http://router.project-osrm.org/table/v1/driving/'
    i=1
    for k,v in coordinates_dict.items():
        if i<len(coordinates_dict):
            url = url+str(v[1])+","+str(v[0])+";"
        else:
            url = url+str(v[1])+","+str(v[0])
        i+=1
    print(url)
    response = requests.get(url)
    jsonRes = response.json()
    rounded_coord_dict = {(round(v[0],2), round(v[1], 2)):k for k,v in coordinates_dict.items()}
    rounded_destinations = [(round(float(x['location'][1]),2), round(float(x['location'][0]), 2)) for x in jsonRes['destinations']]
    routes_dur = {(province_map.get_id(rounded_coord_dict[rounded_destinations[x]]), province_map.get_id(rounded_coord_dict[rounded_destinations[y]])): jsonRes['durations'][x][y] 
                  for x, y in list(it.product(range(len(rounded_destinations)), range(len(rounded_destinations))))}
    return routes_dur

def get_coordinates(prov_id):
    coordinates_df = pd.read_csv('province_coordinates.csv')
    
    dict_map = {p:province_map.get_id(p) for p in list(coordinates_df['Province Name'])}
    coordinates_df["p_id"] = coordinates_df["Province Name"].map(dict_map)
    
    Lat = coordinates_df.loc[coordinates_df["p_id"] == prov_id, 'Latitude'].values[0]
    Long = coordinates_df.loc[coordinates_df["p_id"] == prov_id, 'Longitude'].values[0]
    return(Lat, Long)

def get_bearing(p1, p2):
    
    long_diff = np.radians(p2.lon - p1.lon)
    
    lat1 = np.radians(p1.lat)
    lat2 = np.radians(p2.lat)
    
    x = np.sin(long_diff) * np.cos(lat2)
    y = (np.cos(lat1) * np.sin(lat2) 
        - (np.sin(lat1) * np.cos(lat2) 
        * np.cos(long_diff)))    
    bearing = np.degrees(np.arctan2(x, y))
    
    if bearing < 0:
        return bearing + 360
    return bearing

def get_arrows(locations, color='blue', size=6, n_arrows=1):
    
    Point = namedtuple('Point', field_names=['lat', 'lon'])
    
    p1 = Point(locations[0][0], locations[0][1])
    p2 = Point(locations[1][0], locations[1][1])
    
    rotation = get_bearing(p1, p2) - 90
    
    arrow_lats = np.linspace(p1.lat, p2.lat, n_arrows + 2)[1:n_arrows+1]
    arrow_lons = np.linspace(p1.lon, p2.lon, n_arrows + 2)[1:n_arrows+1]
    
    arrows = []
    
    for points in zip(arrow_lats, arrow_lons):
        arrows.append((points, 
                      color, 3, 
                      size, rotation))
    return arrows
    




def generate_DO_map_data(p1_p2_t_transport_df):
    
    p1_p2_transport_df_grouped = p1_p2_t_transport_df.groupby('location_1_2_index').agg(
    {'location1_name':'first','location2_name':'first',"total_patients_transferred": "sum", 'route_duration':'first', 'cost':'sum'})
    df_temp_list = []
    for t in p1_p2_t_transport_df.date_index.unique():
        if sum(p1_p2_t_transport_df.loc[p1_p2_t_transport_df.date_index == t].total_patients_transferred.values):
            date = p1_p2_t_transport_df.loc[p1_p2_t_transport_df.date_index  == t,'date'].values[0]
            feature_group = folium.FeatureGroup(name='date '+str(date))
            for p1, p2, t in list(p1_p2_t_transport_df.loc[(p1_p2_t_transport_df.total_patients_transferred > 0) & (p1_p2_t_transport_df.date_index == t)].locations_day_index):
                coor_orig = get_coordinates(p1)
                loc_orig_name = p1_p2_transport_df_grouped.loc[p1_p2_transport_df_grouped.index ==(p1, p2), 'location1_name'].values[0]
                coor_dest = get_coordinates(p2)
                loc_dest_name = p1_p2_transport_df_grouped.loc[p1_p2_transport_df_grouped.index ==(p1, p2), 'location2_name'].values[0]
                pat_trans = int(p1_p2_t_transport_df.loc[p1_p2_t_transport_df.locations_day_index ==(p1, p2, t), 'total_patients_transferred'].values[0])
                arrows = get_arrows(locations=[[coor_orig[0], coor_orig[1]], 
                                 [coor_dest[0], coor_dest[1]]], n_arrows=1)
                df_temp_list.append((date, loc_orig_name, coor_orig[0], coor_orig[1], 
                                     loc_dest_name, coor_dest[0], coor_dest[1], pat_trans, (len(arrows),arrows)))

    DO_output_map = pd.DataFrame(df_temp_list, columns=('date', 'loc_orig_name','orig_lat', 'orig_long', 
                                                        'loc_dest_name', 'dest_lat', 'dest_long','num_patnts_trnsf','arrows'))
    return DO_output_map




app = Flask(__name__, static_url_path='')

db_name = 'mydb'
client = None
db = None

# If you want to connect to your COS to obtain results for visualisation, uncomment the lines below and fill in the necessary credentials
#client_to_COS = ibm_boto3.client(service_name='s3',
#    ibm_api_key_id="",
#    ibm_auth_endpoint="https://iam.ng.bluemix.net/oidc/token",
#    config=Config(signature_version='oauth'),
#    endpoint_url="")


# On IBM Cloud Cloud Foundry, get the port number from the environment variable PORT
# When running this app on the local machine, default the port to 8000
port = int(os.getenv('PORT', 8000))

def style_function(feature):
    return {'fillOpacity': 0,'weight': 0.9,'color': 'black','fillColor': 'white'}



def generate_map(DO_output_map, folium_map, map_suffix):

    for date in DO_output_map.date.unique():
        if sum(DO_output_map.loc[DO_output_map.date == date].num_patnts_trnsf.values) > 0:
            feature_group = folium.FeatureGroup(name='date '+str(date))
            for index, row in DO_output_map.loc[DO_output_map.date == date].iterrows():

                folium.map.Marker([row['orig_lat'], row['orig_long']],popup=row['loc_orig_name']).add_to(feature_group)

                folium.map.Marker([row['dest_lat'], row['dest_long']],popup=row['loc_dest_name']).add_to(feature_group)

                folium.PolyLine([[row['orig_lat'], row['orig_long']], 
                                 [row['dest_lat'], row['dest_long']]], tooltip=str(row['num_patnts_trnsf'])+' patients').add_to(feature_group)

                arrows = row['arrows'][1]
                for i in range(row['arrows'][0]):
                    arrow = folium.RegularPolygonMarker(location=arrows[i][0], fill_color= arrows[i][1],
                                                        number_of_sides = arrows[i][2],radius = arrows[i][3], rotation = arrows[i][4])
                    arrow.add_to(feature_group)
                    i+=1

                feature_group.add_to(folium_map)
    folium.LayerControl().add_to(folium_map)
    
    
    folium_map.save('./templates/Belgium_DO_'+map_suffix+'.html')

def json_to_df(filename):
    with open(filename, 'r') as f:
        json_string = json.load(f)
        df_from_json = pd.read_json(json_string)
        df_from_json['locations_day_index'] = df_from_json['locations_day_index'].apply(tuple)
        df_from_json['location_1_2_index'] = df_from_json['location_1_2_index'].apply(tuple)
    return df_from_json

@app.route('/')
def index():
    
#    client_to_COS.download_file(Bucket=COS_BUCKET_NAME, Filename='transfers_scen_1.json', Key='transfers_scen_1.json')
#    client_to_COS.download_file(Bucket=COS_BUCKET_NAME, Filename='transfers_scen_2.json', Key='transfers_scen_2.json')
#    client_to_COS.download_file(Bucket=COS_BUCKET_NAME, Filename='transfers_scen_3.json', Key='transfers_scen_3.json')
    
    
    scenario_1_df = json_to_df('transfers_scen_1.json')
    scenario_2_df = json_to_df('transfers_scen_2.json')
    scenario_3_df = json_to_df('transfers_scen_3.json')

    
    
    scenario_1_map_DF = generate_DO_map_data(scenario_1_df)
    scenario_2_map_DF = generate_DO_map_data(scenario_2_df)
    scenario_3_map_DF = generate_DO_map_data(scenario_3_df)
    
    
    
    Belg_centroid_lat = 50.704896
    Belg_centroid_lon = 4.565249
    url = 'https://vector.maps.elastic.co/files/belgium_provinces_v1.geo.json'

    folium_map_scenario_1 = folium.Map([Belg_centroid_lat, Belg_centroid_lon], zoom_start=8)
    folium.GeoJson(url,name='geojson',style_function=style_function).add_to(folium_map_scenario_1)

    folium_map_scenario_2 = folium.Map([Belg_centroid_lat, Belg_centroid_lon], zoom_start=8)
    folium.GeoJson(url,name='geojson',style_function=style_function).add_to(folium_map_scenario_2)

    folium_map_scenario_3 = folium.Map([Belg_centroid_lat, Belg_centroid_lon], zoom_start=8)
    folium.GeoJson(url,name='geojson',style_function=style_function).add_to(folium_map_scenario_3)
    
    generate_map(scenario_1_map_DF, folium_map_scenario_1, 'scenario_1')
    generate_map(scenario_2_map_DF, folium_map_scenario_2, 'scenario_2')
    generate_map(scenario_3_map_DF, folium_map_scenario_3, 'scenario_3')
    
    return render_template('index.html')

@app.route('/map')
def show_map(map_label=None):
    map_label = request.args.get('scenario_id', '')
    return render_template('Belgium_DO_'+map_label+'.html')

@atexit.register
def shutdown():
    if client:
        client.disconnect()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=port, debug=True)
