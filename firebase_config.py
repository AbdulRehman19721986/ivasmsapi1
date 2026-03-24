import pyrebase

config = {
    "apiKey": "AIzaSyAUP0Y9-MOxG5iZ5yomYejksEYYd7qb1O4",
    "authDomain": "redxwebsite.firebaseapp.com",
    "databaseURL": "https://redxwebsite-default-rtdb.firebaseio.com",
    "projectId": "redxwebsite",
    "storageBucket": "redxwebsite.firebasestorage.app",
    "messagingSenderId": "625323721041",
    "appId": "1:625323721041:web:abffe65db28d892f359370"
}

firebase = pyrebase.initialize_app(config)
db = firebase.database()
