import json
import time
import os
import requests

BRIGHT_DATA_API_KEY = "6155ab7c-da82-44c6-8332-292a4f514cf4"

TOP_COMPANIES = [
    {"name": "Nvidia", "ticker": "NVDA"},
    {"name": "Microsoft", "ticker": "MSFT"},
    {"name": "Apple", "ticker": "AAPL"},
    {"name": "Amazon", "ticker": "AMZN"},
    {"name": "Alphabet", "ticker": "GOOGL"},
    {"name": "Meta", "ticker": "META"},
    {"name": "Tesla", "ticker": "TSLA"},
    {"name": "Broadcom", "ticker": "AVGO"},
    {"name": "AMD", "ticker": "AMD"},
    {"name": "Netflix", "ticker": "NFLX"},
    {"name": "Palantir", "ticker": "PLTR"},
    {"name": "Oracle", "ticker": "ORCL"},
    {"name": "Salesforce", "ticker": "CRM"},
    {"name": "Adobe", "ticker": "ADBE"},
    {"name": "Uber", "ticker": "UBER"},
    {"name": "Airbnb", "ticker": "ABNB"},
    {"name": "JPMorgan Chase", "ticker": "JPM"},
    {"name": "Visa", "ticker": "V"},
    {"name": "Mastercard", "ticker": "MA"},
    {"name": "Costco", "ticker": "COST"}
]

SOURCES = {
    "reddit": {
        "weight": 0.2,
        "query_template": "site:reddit.com/r/stocks OR site:reddit.com/r/investing {company} {ticker} stock discussion"
    },
    "google_news": {
        "weight": 0.2,
        "query_template": "{company} {ticker} latest stock news earnings"
    },
    "x_twitter": {
        "weight": 0.2,
        "query_template": "site:x.com {company} {ticker} stock"
    },
    "yahoo_finance": {
        "weight": 0.2,
        "query_template": "site:finance.yahoo.com {company} {ticker} stock news"
    },
    "cnbc_news": {
        "weight": 0.2,
        "query_template": "site:cnbc.com {company} {ticker} stock news"
    }
}


def build_queries_for_company(company):
    queries = []

    for source_name, source_info in SOURCES.items():
        query = source_info["query_template"].format(
            company=company["name"],
            ticker=company["ticker"]
        )

        queries.append({
            "company": company["name"],
            "ticker": company["ticker"],
            "source": source_name,
            "weight": source_info["weight"],
            "query": query
        })

    return queries


def search_bright_data(query):
    url = "https://api.brightdata.com/discover"

    headers = {
        "Authorization": f"Bearer {BRIGHT_DATA_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "query": query,
        "mode": "standard",
        "language": "en",
        "country": "US",
        "format": "json",
        "num_results": 5,
        "include_content": False
    }

    post_response = requests.post(url, headers=headers, json=payload, timeout=60)
    print("POST Status:", post_response.status_code, "| Query:", query[:80])

    if post_response.status_code != 200:
        return {
            "error": True,
            "step": "post",
            "status_code": post_response.status_code,
            "message": post_response.text
        }

    post_data = post_response.json()
    task_id = post_data.get("task_id")

    if not task_id:
        return {
            "error": True,
            "message": "No task_id returned",
            "raw_response": post_data
        }

    result_url = f"https://api.brightdata.com/discover?task_id={task_id}"

    for attempt in range(20):
        time.sleep(5)

        get_response = requests.get(result_url, headers=headers, timeout=60)
        print("GET Status:", get_response.status_code, "| attempt:", attempt + 1)

        if get_response.status_code != 200:
            return {
                "error": True,
                "step": "get",
                "status_code": get_response.status_code,
                "message": get_response.text,
                "task_id": task_id
            }

        data = get_response.json()
        print("Current status:", data.get("status"))

        if data.get("status") in ["done", "completed", "ready"]:
            return data

        if "results" in data:
            return data

        print("Still processing...")

    return {
        "error": True,
        "message": "Timeout waiting for Bright Data result",
        "task_id": task_id
    }


def main():
    all_results = []

    # 先只跑 1 家公司测试
    for company in TOP_COMPANIES[:1]:
        print(f"\nCollecting data for {company['name']} ({company['ticker']})")

        queries = build_queries_for_company(company)

        # 先只跑 1 个 source 测试；成功后删掉这一行
        queries = queries[:1]

        for item in queries:
            result = search_bright_data(item["query"])

            evidence = {
                "company": item["company"],
                "ticker": item["ticker"],
                "source": item["source"],
                "weight": item["weight"],
                "query": item["query"],
                "bright_data_result": result
            }

            all_results.append(evidence)

    output_file = os.path.join(
    os.path.dirname(__file__),
    "stock_evidence.json"
)

    with open(output_file, "w", encoding="utf-8") as f:
       json.dump(all_results, f, indent=2, ensure_ascii=False)

    print("\nDone.")
    print("Saved to:", output_file)


if __name__ == "__main__":
    main()