import json
import os
import time
import numpy as np
import redis
import settings
import pandas as pd
import pickle
from shapely.geometry import Point, Polygon
from geopy.geocoders import Nominatim
from shapely import wkt
from shapely.geometry import Point
import openmeteo_requests
import requests_cache
from retry_requests import retry
from datetime import datetime

# Setup the Open-Meteo API client with cache and retry on error
cache_session = requests_cache.CachedSession('.cache', expire_after=3600)
retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
openmeteo = openmeteo_requests.Client(session=retry_session)

# Connect to Redis and assign to variable `db`
db = redis.Redis(host=settings.REDIS_IP,
                 port=settings.REDIS_PORT, db=settings.REDIS_DB_ID)

# Check Redis connection
try:
    db.ping()
    print("Connected to Redis successfully!")
except redis.ConnectionError:
    print("Failed to connect to Redis.")

# Load models from pickle files
with open('xgb_fare.pkl', 'rb') as f:
    fare_model = pickle.load(f)
    print("Loaded fare model successfully.")

with open('duration_model.pkl', 'rb') as f:
    duration_model = pickle.load(f)
    print("Loaded duration model successfully.")

# Load taxi zones geometry data
df_zones = pd.read_csv('taxis_zones_geometry.csv')
print("Loaded taxi zones geometry data successfully.")

def predict_fare(features):
    """
    Predict the fare amount using the loaded model.
    """
    return fare_model.predict([features])[0]

def predict_duration(features):
    """
    Predict the trip duration using the loaded model.
    """
    return duration_model.predict([features])[0]

def encontrar_zona_taxi(coord_x, coord_y):
    """
    Encuentra la zona de taxi en la que se encuentra una coordenada.
    """
    point = Point(coord_x, coord_y)
    print(f"Finding taxi zone for coordinates: {coord_x}, {coord_y}")
    for index, row in df_zones.iterrows():
        try:
            polygon = wkt.loads(row['geometry'])
            if polygon.contains(point):
                print(f"Found LocationID: {row['LocationID']} for coordinates: {coord_x}, {coord_y}")
                return row["LocationID"]  # Exit the loop if found
        except Exception as e:
            print(f"Error processing polygon: {e}")
            continue
    print(f"No LocationID found for coordinates: {coord_x}, {coord_y}")
    return None

def get_coordinates(location_name):
    """
    Translates a location name into latitude and longitude coordinates.

    Args:
        location_name: The name of the location (e.g., "Empire State Building").

    Returns:
        A tuple containing the latitude and longitude coordinates, or None if the 
        location is not found.
    """
    geolocator = Nominatim(user_agent="my_geocoder")  # Provide a user agent
    location = geolocator.geocode(location_name)
    if location:
        print(f"Coordinates for {location_name}: {location.latitude}, {location.longitude}")
        return location.longitude, location.latitude 
    else:
        print(f"Coordinates not found for {location_name}")
        return None

def get_weather_data():
    """
    Fetches the current weather data for New York City.

    Returns:
        A dictionary containing the weather data.
    """
    latitude = 40.7143
    longitude = -74.006
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ["temperature_2m", "weather_code", "rain", "showers", "snowfall", "snow_depth"],
        "current_weather": True,
      	"current": ["weather_code", "is_day", "precipitation", "temperature_2m"],
        "timezone": "America/New_York"
    }
    response = openmeteo.weather_api(url, params=params)[0]
    current = response.Current()
    hourly = response.Hourly()
    current_temperature=current.Variables(3).Value()
    current_precipitation = current.Variables(2).Value()						
    current_weather_code = current.Variables(0).Value()
    current_is_day = current.Variables(1).Value()
    hourly_temperature_2m = hourly.Variables(0).ValuesAsNumpy()[0]
    hourly_rain = hourly.Variables(1).ValuesAsNumpy()[0]
    hourly_showers = hourly.Variables(2).ValuesAsNumpy()[0]
    hourly_snowfall = hourly.Variables(3).ValuesAsNumpy()[0]
    hourly_snow_depth = hourly.Variables(4).ValuesAsNumpy()[0]
    hourly_weather_code = hourly.Variables(5).ValuesAsNumpy()[0]

    # Get current time details
    current_time = datetime.now()
    current_hour = current_time.strftime("%Y-%m-%dT%H:00")

    # Find the index of the current hour in the hourly data
    print(f"Weather data for New York City at {current_hour}: {current_weather_code}, {current_is_day}, {hourly_temperature_2m}, {hourly_rain}, {hourly_showers}, {hourly_snowfall}, {hourly_snow_depth}, {hourly_weather_code}")
    return {
        "rain": hourly_rain,
        "snowfall": hourly_snowfall,
        "weather_code": current_weather_code,
        "snow_depth": hourly_snow_depth,
        "is_day": current_is_day,
        "showers":hourly_showers,
        "temperature":current_temperature,
        "precipitation":current_precipitation
    }

def classify_process():
    """
    Loop indefinitely asking Redis for new jobs.
    When a new job arrives, takes it from the Redis queue, uses the loaded ML
    model to get predictions and stores the results back in Redis using
    the original job ID so other services can see it was processed and access
    the results.
    """
    while True:
        # Take a new job from Redis
        print("Waiting for new job...")
        job_data = db.get("formData")
        if job_data:
            job_data = json.loads(job_data.decode('utf-8'))
            print(f"Received job data: {job_data}")

        # Extract the necessary data from the job
            form_data = job_data
            start_point = form_data["startPoint"]
            end_point = form_data["endPoint"]
            passenger_count = int(form_data["passengerCount"])
            duration = form_data["duration"]
            distance_km = form_data["distance"]
            distance_miles = distance_km * 0.621371  # Convert distance to miles

        # Get coordinates for start and end points
            start_coords = get_coordinates(start_point)
            end_coords = get_coordinates(end_point)

            if start_coords and end_coords:
            # Find LocationID for start and end points
                start_location_id = encontrar_zona_taxi(start_coords[0], start_coords[1])
                end_location_id = encontrar_zona_taxi(end_coords[0], end_coords[1])

                if start_location_id and end_location_id:
                # Get weather data for New York City
                    weather_data = get_weather_data()

                # Get current time details
                    current_time = datetime.now()
                hour = current_time.hour
                month = current_time.month
                day_of_week = current_time.weekday()
    
                # Prepare features for prediction
                fare_features = [
                         distance_miles,  # trip_distance
                    1,  # payment_type (assuming 1 for simplicity)
                    weather_data["rain"],  # rain
                    weather_data["snowfall"],  # snowfall
                    passenger_count,  # passenger_count
                    weather_data["weather_code"],  # weather_code


                    weather_data["precipitation"], # precipitation (assuming 0 for simplicity)
                    hour,  # h
                    month,  # m
                    day_of_week,  # day_of_week
                    weather_data["is_day"] 
                ]
                duration_features = [
                    distance_miles,  # trip_distance
                    start_location_id,  # PULocationID
                    end_location_id,  # DOLocationID
                    weather_data["rain"],  # rain
                    weather_data["snowfall"],  # snowfall
                    weather_data["weather_code"],  # weather_code
                    weather_data["snow_depth"],  # snow_depth
                    hour,  # h
                    day_of_week,  # day_of_week
                    month,  # m
                    weather_data["is_day"]  # is_day
                ]
    
                    # Run predictions
                print(f"Running fare prediction with features: {fare_features}")
                fare_prediction = predict_fare(fare_features)
                print(f"Fare prediction: {fare_prediction}")
    
                print(f"Running duration prediction with features: {duration_features}")
                duration_prediction = predict_duration(duration_features)
                print(f"Duration prediction: {duration_prediction}")
    
                    # Prepare the output
                output = {
                        "fare": round(float(fare_prediction*10),2),
                        "duration": round(float(duration_prediction*10),2)
                    }
    
                    # Store the results on Redis using the original job ID as the key
                db.set("tripPredict", json.dumps(output))
                db.delete("formData")  # Delete the job from the queue
                print(f"Stored prediction results in Redis: {output}")
            else:
                print("Error processing job data. Skipping...")
        # Sleep for a bit
        time.sleep(settings.SERVER_SLEEP)
         

if __name__ == "__main__":
    # Now launch process
    print("Launching ML service...")
    classify_process()
