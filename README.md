# shotgrid-webhooks-firebase

![statuses_2](https://github.com/user-attachments/assets/c919d1ba-07f9-4721-b19e-8a46b364de37)

🚀 Ported Webhooks application from @sinclairtarget who did the original work for hosting on a windows machine on prem.

Functions are hosted on firebase and endpoints can be reachable by setting up ShotGrid's webhooks workflow.

Firebase Docs: https://firebase.google.com/docs/cli

## Features

- **task_webhook**: Handles ShotGrid Task status change events.
- **version_webhook**: Handles ShotGrid Version status change events.
- **version_created_webhook**: Handles ShotGrid Version creation events.
- **Local Testing**: A `main` function for testing via the Functions Framework.

## Project Structure

```
functions/
├── config.json            # ShotGrid & secret token configuration
├── status_mapping.yaml    # Version statuses & task relations
├── requirements.txt       # Python dependencies
├── main.py                # Cloud Functions entrypoints & dispatch logic
├── .firebaserc            # Firebase project settings
├── .gitignore             # Ignored files
└── venv/                  # Python virtual environment
```

## To make changes:
Get to the right place:
```
cd functions
```
Create environment:
```
python -m venv .venv
```
Activate environment:
```
.venv/bin/activate
```
Grab those dependencies:
```
pip install -r requirements.txt
```
Deploy:
```
firebase deploy --only functions
```
Logs:
```
firebase functions:log
```

## Configuration

1. **Copy `config.json` and update values**:

   ```json
   {
     "SHOTGRID_API_KEY": "<your_api_key>",
     "SHOTGRID_SCRIPT_NAME": "<your_script_name>",
     "SECRET_TOKEN": "<your_secret_token>",
     "SHOTGRID_URL": "https://your.shotgrid.url"
   }
   ```

2. **Adjust `status_mapping.yaml` to match your ShotGrid status keys and labels, and define any task-step relationships**:

   ```yaml
   version_statuses:
     - key: na
       label: N/A
     - key: stcomp
       label: Step Completed
     # ... more statuses
     task_step_relations:
     Rotoscoping:
       triggers_on_status: stcomp
       update_steps:
         - Composite
         - Secondary Composite
       new_status: bfr
     # ... more relations
   ```

## Usage

Check endpoints based on Firebase configuration.

- **task_webhook**: Deployed URL `/task_webhook` receives task status change webhooks.
- **version_webhook**: Deployed URL `/version_webhook` receives version status change webhooks.
- **version_created_webhook**: Deployed URL `/version_created_webhook` receives new version creation webhooks.

Each endpoint verifies the `SECRET_TOKEN` header, parses the JSON payload, and dispatches logic to ShotGrid per `status_mapping.yaml` rules.

## Deployment

1. **Log in to Firebase**:

   ```bash
   firebase login
   ```

2. **Initialize Firebase (if not already done)**:

   ```bash
   firebase init functions
   ```

3. **Deploy functions (Gen-2)**:

   ```bash
   firebase deploy --only functions
   ```
