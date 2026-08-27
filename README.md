# Quiz Module for NeuraNote

## Overview

NeuraNote Quiz Module is an intelligent quiz generation system that leverages AI to create personalized quizzes from study materials. This module enables students to test their knowledge through adaptive difficulty levels and track their progress with comprehensive analytics.

### **Key Highlights**

-  **AI-Powered Generation**: Leverages AI to create contextual questions from document and image files.
-  **Progressive Learning**: Easy → Medium → Hard level progression system
-  **Analytics Dashboard**: Comprehensive performance tracking and insights

##  Features

### **Core Functionality**

#### Quiz Generation
- **AI-Powered Questions**: Generates contextual questions using OpenAI GPT-3.5-turbo
- **Multiple Question Types**:
  - Multiple Choice Questions (MCQ)
  - Short Answer Questions
  - Mixed (both types)
- **Flexible Configuration**:
  - 1-25 questions per quiz
  - Time limits: 1-180 minutes
  - Difficulty levels: Easy, Medium, Hard
- **Multi-Format Support**: Upload up to 20 files (25MB each)
  - Documents: PDF, DOC, DOCX, TXT
  - Spreadsheets: XLSX, XLS
  - Presentations: PPT, PPTX
  - Images: JPG, PNG, GIF, WEBP
  - eBooks: EPUB

#### Quiz Taking Experience
- **Live Timer**: Real-time countdown with auto-submit
- **Question Navigation**: Grid-based navigation with answered/unanswered indicators
- **Progress Tracking**: Visual progress bar showing completion status
- **Answer Review**: Color-coded feedback (correct/incorrect/needs review)
- **Cancel Quiz**: Exit ongoing quiz anytime

#### Analytics & History
- **Quiz History**: Complete record of all quiz attempts with:
  - Scores and difficulty levels
  - Time taken and date stamps
  - Pass/fail indicators (50% threshold)
  - Pagination support (10 per page)
- **Analytics Dashboard**:
  - Overall Performance: Average score, best score, pass rate
  - Performance by Difficulty: Stats for Easy/Medium/Hard
  - Recent Trend Chart: Visual graph of last 7 attempts
  - Top 5 Performances: Leaderboard of best scores
  - Time Statistics: Average, fastest, slowest completion times
- **Detailed Review**: Question-by-question breakdown with user answers vs correct answers

#### Progressive Learning
- **Level Progression**: 
  - Start with Easy level
  - Progress to Medium after completing Easy
  - Unlock Hard level after Medium
  - Single quiz per level using same source content
- **Smart Difficulty Adjustment**: Questions adapt based on difficulty level
- **Achievement Tracking**: Visual progression path (⭐ Easy → ⭐⭐ Medium → ⭐⭐⭐ Hard)
  
#### Additional Features
- **PDF Export**: Download quiz results as formatted PDF
- **Toast Notifications**: User-friendly success/error/info messages
- **File Validation**: Automatic type and size checking
