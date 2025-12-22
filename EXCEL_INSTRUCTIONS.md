# How to Use Your Refrigeration App with Excel

## Step 1: Find Your App's Web Address

1. Look at the top-right area of your Replit screen
2. Find the "Deployments" button or section
3. Click on it - you'll see your live URL
4. It looks something like: `https://workspace-yourname.replit.app`
5. **Copy this URL** - you'll need it in the next steps

---

## Method 1: Using Excel Power Query (Easiest - No Coding)

### Step-by-Step:

**1. Open Excel** (any version from 2016 or newer)

**2. Get Your Data:**
   - Click the **"Data"** tab at the top of Excel
   - Click **"Get Data"** or **"From Web"** button
   - (Exact button name depends on your Excel version)

**3. Enter Your URL:**
   - A dialog box appears asking for a URL
   - Type your deployment URL and add `?format=json` at the end
   - Example: `https://workspace-yourname.replit.app/?format=json`
   - Click **OK**

**4. Excel Will Load Your Data:**
   - Excel connects to your app
   - You'll see all your saved refrigeration project data
   - Power Query will show the data structure

**5. Expand the Data:**
   - Click the expand arrows (⬇️) next to column headers
   - Select which fields you want (customer, address, equipment, etc.)
   - Click **OK**

**6. Load to Excel:**
   - Click **"Close & Load"**
   - Your data appears as a nice Excel table!

**7. Refresh Anytime:**
   - Right-click the table
   - Click **"Refresh"**
   - Excel pulls the latest data from your app

---

## Method 2: Using Excel VBA (For Advanced Users)

### Step-by-Step:

**1. Open Excel's Developer Tab:**
   - If you don't see "Developer" tab at the top:
     - Go to **File** → **Options** → **Customize Ribbon**
     - Check the **"Developer"** box on the right
     - Click **OK**

**2. Open VBA Editor:**
   - Click the **Developer** tab
   - Click **"Visual Basic"** button
   - Or press **Alt + F11**

**3. Insert a New Module:**
   - In VBA editor, click **Insert** → **Module**
   - A blank code window appears

**4. Paste This Code:**
```vba
Function GetProposalTotal() As String
    Dim http As Object
    Set http = CreateObject("MSXML2.XMLHTTP")
    
    ' REPLACE THIS URL with your actual deployment URL
    http.Open "GET", "https://your-deployment-url/?format=json", False
    http.send
    
    If http.Status = 200 Then
        Dim body As String
        body = http.responseText
        
        ' Find the "total" value in the JSON
        Dim startPos As Long, endPos As Long
        startPos = InStr(body, """total"":")
        If startPos > 0 Then
            startPos = InStr(startPos, body, ":") + 1
            endPos = InStr(startPos, body, ",")
            GetProposalTotal = Trim(Replace(Mid(body, startPos, endPos - startPos), """", ""))
        Else
            GetProposalTotal = ""
        End If
    Else
        GetProposalTotal = ""
    End If
End Function
```

**5. Replace the URL:**
   - Find the line: `http.Open "GET", "https://your-deployment-url/?format=json", False`
   - Replace `https://your-deployment-url` with your actual deployment URL

**6. Use in Excel:**
   - Go back to your Excel spreadsheet
   - In any cell, type: `=GetProposalTotal()`
   - Press **Enter**
   - The function pulls data from your app!

---

## Which Method Should You Use?

### Use **Method 1 (Power Query)** if:
- ✅ You just want to pull all your project data into Excel
- ✅ You want to refresh it easily
- ✅ You don't know VBA programming
- ✅ You want the simplest option

### Use **Method 2 (VBA)** if:
- You need custom functions in Excel
- You want to pull specific pieces of data
- You're comfortable with Excel macros
- You want to automate complex workflows

---

## Troubleshooting

**"Can't connect to web"**
- Make sure your deployment URL is correct
- Check that `?format=json` is at the end
- Verify your app is published and running

**"No data showing"**
- First enter some data on the website
- Save at least one project
- Then try pulling into Excel again

**"Permission denied"**
- Excel's security might be blocking web connections
- Go to **File** → **Options** → **Trust Center** → **Trust Center Settings**
- Check your privacy and external content settings

---

## Quick Summary

1. **Find your deployment URL** in Replit
2. **For simple use:** Data → From Web → Your URL + `?format=json`
3. **Click Refresh** in Excel anytime to get latest data

That's it! Your field data flows right into Excel.
