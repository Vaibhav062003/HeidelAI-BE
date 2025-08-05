from agents.customer_agent.graph import CHAT_AGENT
from langchain_core.messages import HumanMessage, AIMessage

if __name__ == "__main__":
    conversation_history = []

    while True:
        msg = input("User: ")

        if (msg.lower() == "exit" or
            msg.lower() == "quit" or
            msg.lower() == "q"
        ):
            print("Exiting the agent.")
            break

        conversation_history.append(HumanMessage(content=msg))

        inputs = {"messages": conversation_history}
        result = CHAT_AGENT.invoke(inputs)
        
        if result.get("final_response"):
            print("Final Response:", result["final_response"])
            conversation_history.append(AIMessage(content=result["final_response"].summary))
        else:
            print("Agent Response:", result["messages"][-1].content)
            conversation_history.append(result["messages"][-1])

