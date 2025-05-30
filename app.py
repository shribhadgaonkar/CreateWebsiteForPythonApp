from flask import Flask, render_template, request, jsonify
import subprocess
import os
import shutil
import sys
import time
import requests
import random
import string

app = Flask(__name__)
DEPLOYED_APP_PORT = 8080
CLONE_DIR = 'temp_cloned_app'
DOCKER_IMAGE_NAME = 'user_deployed_app_image'
DOCKER_CONTAINER_NAME = 'user_deployed_app_container'

HEROKU_API_KEY = os.environ.get('HEROKU_API_KEY')
HEROKU_API_BASE_URL = 'https://api.heroku.com'
HEROKU_HEADERS = {
    'Accept': 'application/vnd.heroku+json; version=3',
    'Authorization': f'Bearer {HEROKU_API_KEY}',
    'Content-Type': 'application/json',
}

current_heroku_app_info = {} # Store {'name': 'app-name', 'url': 'app-url'}

def update_heroku_auth_header():
    # This function is called before each request to ensure HEROKU_HEADERS is up-to-date
    # especially if HEROKU_API_KEY might be set after initial app load (e.g., in some testing scenarios or delayed config)
    global HEROKU_HEADERS # Allow modification of global HEROKU_HEADERS
    global HEROKU_API_KEY   # Allow modification of global HEROKU_API_KEY
    env_api_key = os.environ.get('HEROKU_API_KEY')

    if env_api_key != HEROKU_API_KEY: # If key changed or was set
        HEROKU_API_KEY = env_api_key # Update the global variable for other uses
        if env_api_key:
            HEROKU_HEADERS['Authorization'] = f'Bearer {env_api_key}'
            # print(f'DEBUG: Updated HEROKU_HEADERS with API key ending: {env_api_key[-4:] if env_api_key else "None"}') # For debugging
        else: # API key was unset
            HEROKU_HEADERS['Authorization'] = 'Bearer None' # Or handle as error
            # print('DEBUG: Cleared API key from HEROKU_HEADERS.')


@app.before_request
def before_request_hook():
    update_heroku_auth_header()
    # The warning about HEROKU_API_KEY missing for Heroku deployments is now primarily handled
    # within the deploy() route for more direct user feedback.
    # A general startup warning is also in if __name__ == '__main__':

@app.route('/')
def index():
    return render_template('index.html', current_heroku_app_name=current_heroku_app_info.get('name'))

def stop_and_remove_local_container(container_name):
    subprocess.run(['docker', 'stop', container_name], capture_output=True, text=True)
    subprocess.run(['docker', 'rm', container_name], capture_output=True, text=True)

def generate_heroku_app_name():
    prefix = 'pydeployer-app-'
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return prefix + random_suffix

def run_subprocess(command, step_name, cwd=None):
    """Helper function to run subprocesses and handle errors more uniformly."""
    try:
        # print(f"Running {step_name}: {' '.join(command)}") # For debugging
        result = subprocess.run(command, check=True, capture_output=True, text=True, cwd=cwd)
        return result
    except subprocess.CalledProcessError as e:
        error_details = f'Failed during {step_name} (Command: "{e.cmd}").\nReturn Code: {e.returncode}'
        # Limit output length to prevent excessively long messages
        stdout = e.stdout.strip() if e.stdout else ""
        stderr = e.stderr.strip() if e.stderr else ""
        if stdout: error_details += f'\nSTDOUT:\n{stdout[:1000]}' # Show first 1000 chars
        if stderr: error_details += f'\nSTDERR:\n{stderr[:1000]}' # Show first 1000 chars
        raise Exception(error_details) # Re-raise as a generic Exception

@app.route('/deploy', methods=['POST'])
def deploy():
    global current_heroku_app_info
    if request.method == 'POST':
        git_url = request.form.get('git_url')
        deploy_target = request.form.get('deploy_target', 'local')

        if not git_url: return jsonify({'status': 'error', 'message': 'Git URL is required'}), 400

        # Use the potentially updated HEROKU_API_KEY from the hook for this check
        if deploy_target == 'heroku' and not HEROKU_API_KEY:
            return jsonify({'status': 'error', 'message': 'HEROKU_API_KEY not configured on the server. Cannot deploy to Heroku.'}), 500

        stop_and_remove_local_container(DOCKER_CONTAINER_NAME)
        if os.path.exists(CLONE_DIR): shutil.rmtree(CLONE_DIR)
        os.makedirs(CLONE_DIR, exist_ok=True)

        try:
            run_subprocess(['git', 'clone', git_url, CLONE_DIR], 'Git Clone')

            target_app_file_py = 'app.py'
            if not os.path.exists(os.path.join(CLONE_DIR, target_app_file_py)):
                target_app_file_py = 'main.py'
                if not os.path.exists(os.path.join(CLONE_DIR, target_app_file_py)):
                    return jsonify({'status': 'error', 'message': 'Project structure error: Could not find app.py or main.py in the repository root.'}), 500
            if not os.path.exists(os.path.join(CLONE_DIR, 'requirements.txt')):
                return jsonify({'status': 'error', 'message': 'Project structure error: requirements.txt not found in the repository root.'}), 500

            dockerfile_port_expose = DEPLOYED_APP_PORT
            dockerfile_content = f'''FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
ENV PYTHONUNBUFFERED 1
EXPOSE {dockerfile_port_expose}
CMD ["python", "{target_app_file_py}"]'''
            # App inside container needs to listen on os.environ.get('PORT', default_value_matching_expose)
            # Heroku sets PORT env var. For local, we'll set PORT in docker run.

            with open(os.path.join(CLONE_DIR, 'Dockerfile'), 'w') as f: f.write(dockerfile_content)
            run_subprocess(['docker', 'build', '-t', DOCKER_IMAGE_NAME, '.'], 'Docker Build', cwd=CLONE_DIR)

            if deploy_target == 'heroku':
                heroku_app_name_to_use = current_heroku_app_info.get('name')
                operation_type = 'Updated'

                if not heroku_app_name_to_use:
                    heroku_app_name_to_use = generate_heroku_app_name()
                    create_payload = {'name': heroku_app_name_to_use, 'stack': 'container'}
                    response = requests.post(f'{HEROKU_API_BASE_URL}/apps', json=create_payload, headers=HEROKU_HEADERS, timeout=30)
                    if response.status_code not in [200, 201, 202]:
                        return jsonify({'status': 'error', 'message': f'Heroku API Error (App Create): {response.status_code} - {response.text[:500]}' }), 500
                    heroku_app_info_from_api = response.json()
                    current_heroku_app_info = {'name': heroku_app_name_to_use, 'url': heroku_app_info_from_api.get('web_url')}
                    operation_type = 'Deployed'
                else:
                    response = requests.get(f'{HEROKU_API_BASE_URL}/apps/{heroku_app_name_to_use}', headers=HEROKU_HEADERS, timeout=10)
                    if response.status_code == 404:
                        print(f'Heroku app {heroku_app_name_to_use} not found. Creating a new one.')
                        current_heroku_app_info = {} # Clear old, as it's gone
                        heroku_app_name_to_use = generate_heroku_app_name() # Generate new name
                        create_payload = {'name': heroku_app_name_to_use, 'stack': 'container'}
                        response = requests.post(f'{HEROKU_API_BASE_URL}/apps', json=create_payload, headers=HEROKU_HEADERS, timeout=30)
                        if response.status_code not in [200, 201, 202]: return jsonify({'status': 'error', 'message': f'Heroku API Error (App Re-Create): {response.status_code} - {response.text[:500]}' }), 500
                        heroku_app_info_from_api = response.json()
                        current_heroku_app_info = {'name': heroku_app_name_to_use, 'url': heroku_app_info_from_api.get('web_url')}
                        operation_type = 'Deployed'
                    elif response.status_code not in [200,201,202]:
                         return jsonify({'status': 'error', 'message': f'Heroku API Error (App Info Fetch): {response.status_code} - {response.text[:500]}' }), 500

                run_subprocess(['docker', 'login', '--username=_', f'--password={HEROKU_API_KEY}', 'registry.heroku.com'], 'Docker Login to Heroku')
                heroku_image_tag = f'registry.heroku.com/{current_heroku_app_info["name"]}/web'
                run_subprocess(['docker', 'tag', DOCKER_IMAGE_NAME, heroku_image_tag], 'Docker Tag for Heroku')
                run_subprocess(['docker', 'push', heroku_image_tag], 'Docker Push to Heroku')

                return jsonify({'status': 'success', 'message': f'App {operation_type} to Heroku: {current_heroku_app_info["url"]}', 'app_url': current_heroku_app_info["url"], 'deployed_to': 'heroku', 'app_name': current_heroku_app_info["name"]})

            else: # Local Docker deployment
                # For local, pass PORT env var to container to mimic Heroku and make app code simpler
                docker_run_cmd = ['docker', 'run', '-d', '-p', f'{DEPLOYED_APP_PORT}:{dockerfile_port_expose}', '--env', f'PORT={dockerfile_port_expose}', '--name', DOCKER_CONTAINER_NAME, DOCKER_IMAGE_NAME]
                run_subprocess(docker_run_cmd, 'Docker Run Locally')
                time.sleep(5)
                inspect_result = subprocess.run(['docker', 'inspect', DOCKER_CONTAINER_NAME], capture_output=True, text=True)
                if inspect_result.returncode == 0 and '"Running": true' in inspect_result.stdout:
                    return jsonify({'status': 'success', 'message': f'App deployed locally via Docker on http://localhost:{DEPLOYED_APP_PORT}', 'app_url': f'http://localhost:{DEPLOYED_APP_PORT}', 'deployed_to': 'local'})
                else:
                    logs_result = subprocess.run(['docker', 'logs', DOCKER_CONTAINER_NAME], capture_output=True, text=True)
                    error_msg = f'Local Docker container started but is not healthy or exited.'
                    if logs_result.stdout: error_msg += f'\nSTDOUT:\n{logs_result.stdout.strip()[:1000]}'
                    if logs_result.stderr: error_msg += f'\nSTDERR:\n{logs_result.stderr.strip()[:1000]}'
                    return jsonify({'status': 'error', 'message': error_msg}), 500

        except requests.exceptions.RequestException as e:
            return jsonify({'status': 'error', 'message': f'Network error during Heroku API call: {str(e)}'}), 500
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)}), 500
    return jsonify({'status': 'error', 'message': 'Invalid request method'}), 405

@app.route('/terminate_app', methods=['POST'])
def terminate_app():
    global current_heroku_app_info
    data = request.get_json() if request.is_json else {}
    deployed_to = data.get('deployed_to', 'local')
    app_name_to_terminate = data.get('app_name', current_heroku_app_info.get('name') if deployed_to == 'heroku' else None)

    if deployed_to == 'heroku':
        if not HEROKU_API_KEY: return jsonify({'status': 'error', 'message': 'HEROKU_API_KEY not configured.'}), 500
        if not app_name_to_terminate: return jsonify({'status': 'error', 'message': 'Heroku app name not specified or found for termination.'}), 400

        try:
            response = requests.delete(f'{HEROKU_API_BASE_URL}/apps/{app_name_to_terminate}', headers=HEROKU_HEADERS, timeout=30)
            if response.status_code in [200, 202]:
                if os.path.exists(CLONE_DIR): shutil.rmtree(CLONE_DIR)
                if current_heroku_app_info.get('name') == app_name_to_terminate: current_heroku_app_info = {}
                return jsonify({'status': 'success', 'message': f'Heroku app {app_name_to_terminate} termination initiated.'})
            elif response.status_code == 404:
                if current_heroku_app_info.get('name') == app_name_to_terminate: current_heroku_app_info = {}
                return jsonify({'status': 'success', 'message': f'Heroku app {app_name_to_terminate} already deleted or not found.'})
            else:
                return jsonify({'status': 'error', 'message': f'Heroku API Error (App Delete): {response.status_code} - {response.text[:500]}' }), 500
        except requests.exceptions.RequestException as e:
            return jsonify({'status': 'error', 'message': f'Network error during Heroku API call: {str(e)}'}), 500
    else: # local termination
        stop_and_remove_local_container(DOCKER_CONTAINER_NAME)
        if os.path.exists(CLONE_DIR): shutil.rmtree(CLONE_DIR)
        return jsonify({'status': 'success', 'message': 'Local Dockerized app terminated and cleaned up.'})

if __name__ == '__main__':
    if not os.environ.get('HEROKU_API_KEY'):
        print('STARTUP WARNING: HEROKU_API_KEY environment variable is not set. Heroku deployments will be unavailable until the key is set.')
    app.run(debug=True, port=5000)
