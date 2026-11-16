# IP Checker (Firebase version)

Flask app that shows a visitor's IP address and approximate location, saves the visit to Firebase Firestore, and lets the visitor optionally share their exact location. Built to run on Vercel.

Environment variables (set in Vercel, never commit them):
- FIREBASE_CREDENTIALS: the full contents of the Firebase service-account JSON file
- ADMIN_PASSWORD: password for /admin (username: admin)
