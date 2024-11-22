from flask import Flask, render_template
from sqlalchemy import create_engine, text
import plotly.graph_objs as go
import plotly.io as pio
import plotly.offline as pyo

app = Flask(__name__)
DB_URI="cockroachdb://egenie:mTt-zNDpB35pjYZ7lEotLw@chatbot-5648.7s5.aws-ap-south-1.cockroachlabs.cloud:26257/defaultdb"
db_uri = DB_URI
engine = create_engine(db_uri, pool_pre_ping=True)

# Fetch data from the database
def fetch_data(query):
    with engine.connect() as connection:
        result = connection.execute(text(query))
        return [dict(row) for row in result]

# Fetch response times
def fetch_response_times(platform):
    query = f"SELECT timestamp, time_taken FROM {platform}"
    return fetch_data(query)

# Fetch unique users
def fetch_unique_users(platform):
    if platform == 'whatsapp':
        query = f"SELECT COUNT(DISTINCT name) as unique_users, DATE(timestamp) as date FROM whatsapp GROUP BY DATE(timestamp)"
    else:
        query = f"SELECT COUNT(DISTINCT username) as unique_users, DATE(timestamp) as date FROM {platform} GROUP BY DATE(timestamp)"
    return fetch_data(query)

# Fetch top users
def fetch_top_users(platform):
    if platform == 'whatsapp':
        query = f"SELECT name, COUNT(*) as message_count FROM whatsapp GROUP BY name ORDER BY message_count DESC LIMIT 5"
    else:
        query = f"SELECT username, COUNT(*) as message_count FROM {platform} GROUP BY username ORDER BY message_count DESC LIMIT 5"
    return fetch_data(query)

# Fetch user with most messages
def fetch_user_with_most_messages(platform):
    if platform == 'whatsapp':
        query = f"SELECT name, COUNT(*) as message_count FROM whatsapp GROUP BY name ORDER BY message_count DESC LIMIT 1"
    else:    
        query = f"SELECT username, COUNT(*) as message_count FROM {platform} GROUP BY username ORDER BY message_count DESC LIMIT 1"
    return fetch_data(query)[0]

# Generate line graph for response times
def generate_response_time_graph(platform):
    data = fetch_response_times(platform)
    timestamps = [row['timestamp'] for row in data]
    response_times = [float(row['time_taken']) for row in data]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=timestamps, y=response_times, mode='lines', name=f'{platform} Response Time'))
    fig.update_layout(title=f'{platform.capitalize()} Response Time', xaxis_title='Timestamp', yaxis_title='Response Time (s)')
    return pyo.plot(fig, output_type='div')

# Generate line graph for unique users
def generate_unique_users_graph(platform):
    data = fetch_unique_users(platform)
    dates = [row['date'] for row in data]
    unique_users = [row['unique_users'] for row in data]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=unique_users, mode='lines', name=f'{platform} Unique Users'))
    fig.update_layout(title=f'{platform.capitalize()} Unique Users', xaxis_title='Date', yaxis_title='Unique Users')
    return pyo.plot(fig, output_type='div')


# Fetch daily message counts
def fetch_daily_message_counts(platform):
    query = f"SELECT DATE(timestamp) as date, COUNT(*) as message_count FROM {platform} GROUP BY DATE(timestamp) ORDER BY date"
    return fetch_data(query)

# Generate line graph for daily message counts
def generate_daily_message_counts_graph(platform):
    data = fetch_daily_message_counts(platform)
    dates = [row['date'] for row in data]
    message_counts = [row['message_count'] for row in data]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=message_counts, mode='lines', name=f'{platform} Daily Messages'))
    fig.update_layout(title=f'{platform.capitalize()} Daily Messages', xaxis_title='Date', yaxis_title='Message Count')
    return pyo.plot(fig, output_type='div')

@app.route('/dashboard', methods=['GET'])
def dashboard():
    whatsapp_response_time_graph = generate_response_time_graph('whatsapp')
    instagram_response_time_graph = generate_response_time_graph('instagram')
    whatsapp_unique_users_graph = generate_unique_users_graph('whatsapp')
    instagram_unique_users_graph = generate_unique_users_graph('instagram')
    whatsapp_top_users = fetch_top_users('whatsapp')
    instagram_top_users = fetch_top_users('instagram')
    whatsapp_user_with_most_messages = fetch_user_with_most_messages('whatsapp')
    instagram_user_with_most_messages = fetch_user_with_most_messages('instagram')
    whatsapp_daily_message_counts_graph = generate_daily_message_counts_graph('whatsapp')
    instagram_daily_message_counts_graph = generate_daily_message_counts_graph('instagram')

    return render_template('dashboard.html',
                           whatsapp_response_time_graph=whatsapp_response_time_graph,
                           instagram_response_time_graph=instagram_response_time_graph,
                           whatsapp_unique_users_graph=whatsapp_unique_users_graph,
                           instagram_unique_users_graph=instagram_unique_users_graph,
                           whatsapp_top_users=whatsapp_top_users,
                           instagram_top_users=instagram_top_users,
                           whatsapp_user_with_most_messages=whatsapp_user_with_most_messages,
                           instagram_user_with_most_messages=instagram_user_with_most_messages,
                           whatsapp_daily_message_counts_graph=whatsapp_daily_message_counts_graph,
                           instagram_daily_message_counts_graph=instagram_daily_message_counts_graph)
if __name__ == '__main__':
    app.run(debug=True)