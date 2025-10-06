Inbound Carrier Assistant – Complete Technical Documentation
1. Introduction

The Inbound Carrier Assistant is an intelligent, AI-powered solution designed to automate inbound carrier calls in the logistics sector. It replicates the natural flow of communication between a freight brokerage and a carrier, performing the same tasks a human broker would do but fully automated. The system verifies the carrier’s MC number, presents available loads, negotiates freight rates based on realistic market behavior, and logs all results for performance analysis. Each call produces a complete record that can be viewed in a live metrics dashboard, allowing clear oversight of negotiations, pricing, and agent performance.

This project demonstrates how conversational AI can integrate seamlessly with a backend system to deliver a production-quality prototype for logistics automation. It was developed as part of the HappyRobot technical process, combining three main layers: a FastAPI backend for business logic, a workflow in the HappyRobot platform for natural voice interaction, and a live HTML dashboard for data visualization. The overall objective was to build a realistic, secure, and scalable inbound assistant that could handle carrier calls from start to finish, reducing manual workload while maintaining professional communication and accurate pricing control.



2. System Architecture

The architecture of the system is structured around three connected components: the FastAPI backend, the HappyRobot conversational workflow, and the real-time dashboard.

The backend serves as the central logic engine of the project. It is responsible for all carrier verification, price negotiation, data logging, and performance aggregation. It exposes several API endpoints that are consumed by the HappyRobot workflow during each conversation. Every step in the voice interaction—such as authentication, load presentation, negotiation, and logging—triggers an API call to the backend.

The HappyRobot workflow acts as the voice interface. It receives inbound calls, asks for the carrier’s MC number, verifies eligibility, and manages the dialogue for negotiation. The voice agent follows a structured conversational flow that mirrors how a human broker would interact, but with the ability to make decisions instantly using the backend’s logic.

The dashboard connects directly to the backend’s data layer and visualizes all call results and business metrics. It is continuously refreshed in the background, showing key information such as total calls, accepted negotiations, rejected offers, acceptance rate, total and accepted revenue, and pricing accuracy relative to the listed board rates.

The entire system is deployed through Docker on Railway. The backend is served over HTTPS, and all requests are authenticated via API keys, ensuring security and controlled access.



3. Backend Implementation

The backend, written in Python using the FastAPI framework, handles the complete decision-making process of the application. It uses asynchronous HTTP endpoints to ensure fast communication with the HappyRobot platform.

Carrier Authentication

The /api/authenticate endpoint validates the MC number provided by the carrier during the call. When a real FMCSA API key is available, the system connects directly to the FMCSA database to retrieve official carrier information. If the FMCSA service returns an error (for example, a “403 Forbidden” status caused by rate limits or key restrictions), the system automatically falls back to a mock response. This ensures that the demonstration remains fully functional under any conditions. The response contains an eligibility flag, the carrier’s name, operating status, and other relevant details.

Load Retrieval

Once a carrier has been verified, the workflow calls the /api/loads endpoint. This endpoint reads a local JSON file named loads.json that contains sample freight data. Each entry in the file defines a load with attributes such as load_id, origin, destination, pickup_datetime, delivery_datetime, equipment_type, loadboard_rate, and miles. The endpoint can also accept optional filters for origin, destination, or maximum miles, returning up to ten loads that match the criteria. In the workflow, the first available load is selected for negotiation.

Negotiation Logic

The negotiation process is managed by the /api/negotiate endpoint, which models realistic broker-carrier interactions. In the logistics industry, carriers typically ask for higher prices than the broker’s listed rate. To reflect this, the backend includes a pricing rule that compares the offered price to the load’s listed “board rate.” The parameter MAX_OVER_PCT defines the acceptable threshold above the board rate, set to ten percent by default.

If the carrier’s offer is within this threshold (for example, $1,600 offered for a $1,500 board rate), the system accepts it. If the offer exceeds the limit (for instance, $1,800 for a $1,500 load), the backend responds with a counteroffer at the maximum allowed rate, which would be $1,650. The process can repeat for up to three rounds, after which the backend automatically rejects the negotiation if no agreement has been reached. Each negotiation state—round number, offered price, counteroffer, and outcome—is stored in memory and used to update performance metrics.

Call Result Logging

When the negotiation ends, the /api/call/result endpoint is called to log the outcome. This endpoint records the complete interaction, including the carrier’s MC number, load ID, transcript summary, final price, acceptance status, sentiment, and board rate. It uses a simple keyword-based sentiment analysis to classify each conversation as positive, negative, or neutral. The resulting data is appended to an in-memory list, serving as a temporary storage for all call results. In a production setting, this would be replaced by a persistent database such as PostgreSQL.

Metrics and Dashboard Integration

The /api/metrics endpoint calculates aggregated statistics used by the dashboard. It measures the number of total calls, accepted and rejected negotiations, average negotiation rounds, acceptance rate, and revenue values. The /dashboard endpoint serves a live HTML interface that retrieves this data periodically from the backend through AJAX calls. The frontend visualizes it using dynamically rendered charts.


4. The HappyRobot Workflow

The conversational workflow was built entirely inside the HappyRobot platform using the “Inbound Voice Agent” as the entry point. The flow starts automatically when a carrier joins a call through the web interface or a connected phone number.

The agent first greets the caller and requests the MC number. When the carrier provides it, the tool named Authenticate Carrier is triggered, sending a POST request to the backend’s /api/authenticate endpoint. If the carrier is eligible, the agent confirms verification and proceeds to retrieve available loads using a GET request to /api/loads.

The AI then presents the first load from the list, describing its origin, destination, pickup and delivery times, equipment type, and board rate. The conversation continues naturally as the AI asks, “What rate can you do on this load?” The carrier’s spoken reply is automatically processed, and the value is passed to the backend through the Negotiate Offer tool, which connects to /api/negotiate.

The backend’s response determines the next step of the conversation. If the offer is accepted, the AI says “Great, we can book this load at your rate,” and logs the result. If the backend returns a counteroffer, the AI responds conversationally, for example: “I can do $1,650. Would that work for you?” The loop continues until an agreement is reached or the maximum round limit is exceeded. If the negotiation ends without agreement, the AI politely closes the call, explaining that no deal could be made. The final stage, Log Result, sends the last summary to /api/call/result, ensuring the backend updates all relevant statistics and the dashboard reflects the new data.

The conversational prompts were carefully designed to keep the dialogue short, polite, and realistic. The AI maintains a consistent tone, confirms numbers back to the caller, and never improvises data. Its responses are professional but friendly, creating an authentic brokerage-style interaction.


5. Dashboard and Data Visualization

The dashboard serves as the visual control panel for the system. It provides immediate access to the operational metrics generated by each conversation. Built with simple HTML, CSS, and JavaScript, it connects directly to the backend via AJAX requests that update data every five seconds without refreshing the page.

At the top of the dashboard, key metrics summarize performance, such as the number of calls, accepted and rejected negotiations, total and accepted revenue, and the acceptance rate. The system also calculates two additional insights: the number of accepted deals where the final price exactly matches the listed board price, and the percentage this represents relative to the total calls in the selected date range.

The main visual section contains two graphs. The first is a bar chart that shows the number of accepted and rejected negotiations per day. The bars are intentionally narrow for visual clarity and the legend is displayed inside the chart area to keep both graphs aligned horizontally. The second graph is a pie chart that displays the total sum of accepted revenue compared to overall revenue. Both charts are visually centered and designed to remain stable during updates. Below the charts, the dashboard includes a table listing the most recent calls, with columns for date, MC number, load ID, board rate, final price, acceptance, and sentiment. Users can also filter data by custom date ranges using “from” and “to” selectors or apply quick filters for common ranges such as the last seven, fourteen, or thirty days.

This visualization layer turns the raw data from the backend into meaningful insights, allowing anyone to monitor activity, analyze pricing trends, and measure efficiency in real time.


6. Security and Deployment

All backend routes require an API key specified in the header x-api-key. This ensures that only authorized platforms, such as the HappyRobot workflow, can access or modify the data. The API key, along with other configuration variables, is defined in a .env file that is excluded from version control via .gitignore.

The project is containerized using Docker and deployed on Railway. The Dockerfile installs dependencies, copies the source code and data, and starts the FastAPI application using Uvicorn. The environment variables in Railway define the API key, FMCSA key, and configuration parameters such as LOADS_FILE, MAX_OVER_PCT, and PUBLIC_DASHBOARD. All communication happens over HTTPS to ensure data integrity and privacy.

If the FMCSA API key fails or becomes restricted, the system automatically switches to mock mode, returning realistic sample data to maintain continuity during testing or demonstrations. This design choice guarantees stability and avoids external dependency issues that could interrupt functionality.


7. Results and Conclusion

The Inbound Carrier Assistant successfully reproduces the daily tasks of a carrier sales agent in a fully automated way. It verifies carrier eligibility, presents loads, negotiates rates intelligently, and logs every call with complete transparency. The backend logic ensures that pricing decisions remain consistent and realistic, while the dashboard allows managers to track performance visually.

This prototype shows how conversational AI can transform logistics operations by combining natural dialogue with structured data processing. It eliminates repetitive manual steps, enforces negotiation policies, and provides immediate insight into outcomes through analytics. The modular design also makes it adaptable: it could easily integrate with real TMS systems, databases, or external APIs, and it can be extended with voice transcription, direct call transfers, or outbound campaigns.

In summary, this project stands as a complete proof of concept for inbound logistics automation. It merges voice AI, backend intelligence, and live data visualization into a cohesive system that feels natural to use and performs like a real production service. It demonstrates technical precision, conversational design, and operational awareness—all essential elements for the next generation of AI-driven brokerage tools.
