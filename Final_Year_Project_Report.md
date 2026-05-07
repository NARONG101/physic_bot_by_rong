# Development of a Physics AI Chatbot for Student Learning

## Preliminary Pages

### Abstract in Khmer

ការសិក្សានេះពិចារណាអំពីការអភិវឌ្ឍយន្តការផ្លាស់ប្តូរគន្លងចិត្តប្រើប្រាស់បច្ចេកវិទ្យា AI ដើម្បីជួយសិស្សនៅវិទ្យាសាស្ត្រស៊ីស៊ីល។ គម្រោងនេះមានគោលបំណងបង្កើតឆាតបុតដែលអាចឆ្លើយសំណួរផ្នែកភីស៊ីកបានយ៉ាងត្រឹមត្រូវ និងផ្តល់ពត៌មានពិភាក្សានិងឧទាហរណ៍ធ្វើការយល់ដឹងប្រសើរឡើង។ សៀវភៅតាងតាមការស្រាវជ្រាវនេះបានប្រើប្រាស់បច្ចេកវិទ្យា NLP និងទិន្នន័យផ្នែកវេទិកាសិក្សា ដើម្បីធានាការប្រាស្រ័យទាក់ទងទីអប់រំសមរម្យ និងងាយស្រួល។ វា ក៏ពិចារណាអំពីការវាស់វែងសមត្ថភាពប្រត្តិបត្តិការនិងភាពអាចប្រើបាន បើកទូលាយការសិក្សាយន្តសិក្សា និងផ្តល់នូវការបង្ហាញពីការកែលម្អសមត្ថភាពសិក្សាខណៈសិស្សប្រើប្រាស់ប្រព័ន្ធនេះ។

### Abstract in English

This project explores the development of a Physics AI chatbot designed to support student learning. The study aims to create an intelligent conversational agent capable of answering physics questions accurately and delivering explanations in a student-friendly manner. The system combines natural language processing, knowledge base techniques, and educational design principles to address common physics learning challenges. Evaluation covers response accuracy, usability, and learning improvement, demonstrating how AI chatbots can enhance conceptual understanding and support independent study.

### Supervisor’s Research Supervision Statement

I hereby certify that I have supervised the research and development of the project entitled "Development of a Physics AI Chatbot for Student Learning." The work presented in this report represents the efforts of the candidate and meets the academic standards required for a final year project in Information Technology and Artificial Intelligence.

Supervisor Name: Dr. [Supervisor Name]

Signature: _________________________

Date: _____________________________

### Candidate’s Statement

I declare that this project report is my original work and has not been submitted previously for assessment to any institution. All sources used in the preparation of this report have been acknowledged. The development and research described herein were carried out by me under the supervision of my academic advisor.

Candidate Name: [Student Name]

Signature: _________________________

Date: _____________________________

### Acknowledgements

I would like to express my sincere gratitude to my supervisor, Dr. [Supervisor Name], for their guidance and support throughout this project. I also wish to thank the faculty members, classmates, and participants who provided feedback during system testing. Finally, I acknowledge the encouragement of my family and friends, which made the completion of this project possible.

### Table of Contents

- Preliminary Pages
  - Abstract in Khmer
  - Abstract in English
  - Supervisor’s Research Supervision Statement
  - Candidate’s Statement
  - Acknowledgements
  - Table of Contents
  - List of Figures
  - List of Abbreviations
- CHAPTER 1: INTRODUCTION
  - 1.1 Background to the Study
  - 1.2 Problem Statement
  - 1.3 Aim and Objectives of the Study
  - 1.4 Rationale of the Study
  - 1.5 Limitation and Scope
- CHAPTER 2: LITERATURE REVIEW
  - 2.1 Overview of Physics Education
  - 2.2 AI Chatbots in Education
  - 2.3 Natural Language Processing (NLP)
    - 2.3.1 NLP Techniques in Chatbots
    - 2.3.2 Machine Learning in Chatbots
    - 2.3.3 Comparison of Rule-based vs AI Chatbots
  - 2.4 Evolution of Educational Technologies
  - 2.5 Existing Chatbot Systems
  - 2.6 Theoretical Framework
    - 2.6.1 AI Models
    - 2.6.2 Chatbot Architecture
    - 2.6.3 Data Processing
    - 2.6.4 User Interaction Design
    - 2.6.5 Knowledge Base Systems
- CHAPTER 3: SYSTEM DESIGN AND IMPLEMENTATION
  - 3.1 Current Learning Methods and Challenges
  - 3.2 Proposed Physics AI Chatbot System
  - 3.3 Implementation
    - 3.3.1 Chatbot Architecture
    - 3.3.2 NLP Integration
    - 3.3.3 Database Design
    - 3.3.4 Platform Integration (e.g., Telegram/Web)
    - 3.3.5 Additional Features (Quiz, Image input, etc.)
- CHAPTER 4: RESULTS AND EVALUATION
  - 4.1 System Performance
    - 4.1.1 Accuracy of Responses
    - 4.1.2 User Feedback and Testing
  - 4.2 System Usability
  - 4.3 Evaluation of Learning Improvement
- CHAPTER 5: DISCUSSION
  - 5.1 Conclusion
  - 5.2 Future Work
- REFERENCES
- APPENDICES
  - Appendix A: System Installation and Setup
  - Appendix B: Chatbot Configuration / Code Snippets

### List of Figures

- Figure 1: Proposed chatbot architecture
- Figure 2: Example physics chatbot conversation flow
- Figure 3: System use case for student learning
- Figure 4: Sample quiz interaction in the chatbot

### List of Abbreviations

- AI: Artificial Intelligence
- NLP: Natural Language Processing
- GUI: Graphical User Interface
- API: Application Programming Interface
- ICT: Information and Communication Technology
- ML: Machine Learning
- UI: User Interface
- UX: User Experience
- RAG: Retrieval-Augmented Generation

## CHAPTER 1: INTRODUCTION

### 1.1 Background to the Study

Physics is a core subject in secondary and tertiary education. Many students experience difficulty with abstract concepts, mathematical relations, and problem-solving. Traditional classroom teaching often does not offer enough individualized support, especially when learners study outside school hours. Rapid changes in technology and the need for flexible learning have created an opportunity to use intelligent systems to support student learning.

Artificial Intelligence (AI) and Natural Language Processing (NLP) have enabled new tools that can interact with learners through conversational interfaces. Chatbots are one such tool, providing immediate feedback and explanations in a way that resembles human tutoring. For physics education, chatbots can support concept clarification, answer questions, and provide practice problems. This study examines how an AI chatbot can be designed specifically for physics learners and how it can address the gaps in current learning methods.

### 1.2 Problem Statement

Many physics students struggle with understanding concepts such as motion, forces, energy, and electricity. These difficulties are often linked to the lack of personalized explanations and immediate feedback. Existing learning resources are sometimes too generic, and students may not receive help when they need it most. This project addresses the challenge of providing an accessible, responsive, and subject-specific learning assistant.

The main problem is the absence of a reliable physics-focused conversational learning system that can adapt to student inquiries and explain concepts in clear language. Without such a system, students may rely on memorization instead of achieving deep understanding.

### 1.3 Aim and Objectives of the Study

The aim of this study is to develop a Physics AI chatbot that improves student learning through intelligent conversation and educational support.

The objectives are:
- To design a chatbot architecture tailored for physics learning.
- To integrate NLP techniques for understanding student questions.
- To build a knowledge base containing physics concepts, formulas, and examples.
- To evaluate the chatbot’s performance and usability through student testing.
- To measure the chatbot’s impact on learning improvement.

### 1.4 Rationale of the Study

The rationale for this study lies in the need for accessible learning tools that can support students beyond the classroom. Physics is a subject where students benefit from step-by-step explanations and repeated practice. An AI chatbot can provide these benefits by offering personalized assistance, answering questions at any time, and adapting English explanations to a simple style.

Moreover, using AI in education aligns with global trends toward digital learning. The project demonstrates a practical application of AI and software development skills for an IT final year project, while also creating value for students and teachers.

### 1.5 Limitation and Scope

The study is limited to the development of a chatbot prototype focused on key physics topics suitable for secondary school learners. It does not cover all physics disciplines or advanced university-level topics.

The scope includes:
- Design and implementation of a text-based chatbot interface.
- Integration with a web or messaging platform such as Telegram.
- A knowledge base covering basic mechanics, energy, waves, and electricity.
- User testing with a small sample of learners.

The limitations include:
- Dependence on available training data and predefined knowledge content.
- The chatbot may not handle all types of complex math notation or poorly formed questions.
- Evaluation is limited to initial usability and learning feedback rather than large-scale deployment.

## CHAPTER 2: LITERATURE REVIEW

### 2.1 Overview of Physics Education

Physics education aims to develop scientific reasoning, problem-solving, and conceptual understanding. Students must learn to apply mathematical models to physical situations and interpret experimental results. In many educational contexts, physics is taught through lectures, textbooks, and laboratory work. However, these traditional methods can leave gaps when students encounter difficulties.

Research in physics education has shown that students often struggle with misconceptions and abstract reasoning. Active learning strategies and formative assessment help, but they require additional support resources. Digital learning tools are increasingly used to provide supplementary explanations, visualizations, and interactive practice.

### 2.2 AI Chatbots in Education

AI chatbots have been used in education for tutoring, administrative support, and student engagement. They can answer questions, give reminders, and provide personalized learning paths. In the context of STEM education, chatbots can offer problem-solving guidance and help clarify concepts.

Studies indicate that chatbots can increase learner motivation and reduce anxiety associated with asking questions. They also allow students to learn at their own pace. An effective educational chatbot needs to be accurate, user-friendly, and able to maintain a meaningful dialogue.

### 2.3 Natural Language Processing (NLP)

NLP is the field of AI that focuses on understanding and generating human language. For chatbots, NLP enables the system to interpret user questions and generate appropriate responses. A physics AI chatbot relies on NLP for tasks such as intent detection, entity extraction, and response generation.

#### 2.3.1 NLP Techniques in Chatbots

Common NLP techniques include tokenization, part-of-speech tagging, named entity recognition, and semantic parsing. These techniques help the chatbot break down user input into meaningful units and identify the key concepts being asked about.

For example, a student may ask, "What is the acceleration of an object falling from rest after 3 seconds?" The chatbot must recognize terms like "acceleration," "object falling," "rest," and "3 seconds." It can then use physics formulas to compute an answer and explain the reasoning.

#### 2.3.2 Machine Learning in Chatbots

Machine learning models such as sequence-to-sequence networks, transformers, and intent classification algorithms are widely used in chatbot systems. These models are trained on question-response pairs and can learn patterns in language.

For educational chatbots, supervised learning can be used to classify student intents and match questions to the correct content. Pretrained language models can also generate more natural explanations, though they require careful control to ensure factual accuracy.

#### 2.3.3 Comparison of Rule-based vs AI Chatbots

Rule-based chatbots follow predefined scripts and decision trees. They are predictable and easy to control but may fail when users ask questions outside the scripted paths. AI chatbots use machine learning and NLP to handle a wider range of inputs.

The comparison shows that rule-based chatbots are suitable for narrow tasks, while AI chatbots are better for open-ended educational dialogue. However, AI chatbots require more development effort and careful validation to avoid incorrect answers. A hybrid approach can combine rule-based precision with AI flexibility.

### 2.4 Evolution of Educational Technologies

Educational technologies have evolved from simple instructional media to interactive digital platforms. Early tools included radio and television lessons, followed by computer-based training and learning management systems. Modern technologies now leverage AI, virtual reality, and adaptive learning.

This evolution has shifted the focus from content delivery to learner engagement and personalization. AI chatbots represent a recent stage in this progression, providing conversational access to instructional content and feedback.

### 2.5 Existing Chatbot Systems

Several existing chatbot systems target education, including language learning bots, math tutors, and general study assistants. Systems such as IBM Watson Assistant, Google Dialogflow, and Microsoft Bot Framework offer platforms for creating chatbots.

Research on educational chatbots highlights successful examples in mathematics and language practice. These systems often include question answering, quizzes, and contextual explanations. However, physics-specific chatbots are less common, which motivates the present study.

### 2.6 Theoretical Framework

The theoretical framework for this project combines AI model theory, software architecture, data processing, user interaction design, and knowledge management. These components guide the development of the chatbot and its evaluation.

#### 2.6.1 AI Models

AI models in this project include classification and retrieval models for understanding queries and generating responses. The system may use pretrained language representations or custom supervised models for physics domain understanding.

Key concepts include generalization, semantic similarity, and response selection. The models are designed to map text input into intent categories and knowledge base references.

#### 2.6.2 Chatbot Architecture

Chatbot architecture defines the components that handle input parsing, response generation, dialogue management, and integration with user platforms. A well-structured architecture separates the NLP engine from the knowledge base and user interface.

This separation allows developers to update the physics content without changing the conversation logic. It also supports multiple deployment channels such as web and messaging apps.

#### 2.6.3 Data Processing

Data processing involves preparing physics content, converting it into machine-readable formats, and indexing it for fast retrieval. This includes cleaning text, creating question-answer pairs, and tagging concepts.

In the chatbot system, data processing also covers any logging of user interactions to identify common questions and improve system performance.

#### 2.6.4 User Interaction Design

User interaction design focuses on creating an intuitive and supportive experience for learners. The chatbot should respond clearly, use simple language, and prompt the student when needed.

Design principles include maintaining conversational context, providing examples, and offering follow-up explanations. Visual elements such as buttons or selectable topics can simplify navigation.

#### 2.6.5 Knowledge Base Systems

A knowledge base system stores the domain knowledge used by the chatbot. For physics, this includes definitions, formulas, sample problems, and explanatory text.

The knowledge base can be implemented with structured data, such as JSON records or a database schema. It should support retrieval based on user queries and allow the chatbot to explain concepts with reference materials.

## CHAPTER 3: SYSTEM DESIGN and IMPLEMENTATION

### 3.1 Current Learning Methods and Challenges

Current learning methods for physics often rely on classroom lectures, textbooks, and teacher-led problem solving. While these methods are valuable, they may not provide enough time for individualized support. Students may also face difficulties in finding explanations that match their level of comprehension.

Challenges include:
- Lack of immediate personalized feedback.
- Difficulty in understanding abstract concepts.
- Limited access to practice questions with guided solutions.
- Reliance on memorization instead of understanding.

Digital learning tools can help, but many existing platforms are not specialized for physics or do not support conversational learning.

### 3.2 Proposed Physics AI Chatbot System

The proposed system is a Physics AI chatbot designed to support learners through conversation. It will:
- Understand student questions using NLP.
- Retrieve physics knowledge from a structured database.
- Provide clear explanations and examples.
- Offer quiz questions and interactive feedback.
- Support deployment on a web environment or messaging platform.

The system is intended as a supplementary learning assistant. It is not a replacement for formal teaching but a tool to reinforce understanding and encourage independent study.

### 3.3 Implementation

The implementation of the chatbot includes architecture design, NLP integration, database design, platform integration, and additional features.

#### 3.3.1 Chatbot Architecture

The chatbot architecture consists of the following layers:
- User Interface layer: collects user input and displays responses.
- NLP layer: processes user text and identifies intents and entities.
- Knowledge Base layer: stores physics content and retrieval logic.
- Dialogue Manager: coordinates the conversation state and response selection.

Figure 1 illustrates the architecture, showing how user queries flow through the NLP engine to the knowledge base and back to the user.

#### 3.3.2 NLP Integration

NLP integration is essential for interpreting student questions. The system uses standard NLP tasks such as tokenization, intent classification, and entity extraction.

Example workflow:
1. Student enters a question: "How do I calculate kinetic energy?"
2. The NLP layer identifies the intent as a physics inquiry and the entity "kinetic energy."
3. The chatbot retrieves the relevant definition and formula from the knowledge base.
4. The system returns a clear explanation and an example calculation.

The chatbot may use lightweight machine learning models for classification and retrieval, or integrate with existing NLP libraries.

#### 3.3.3 Database Design

The database design stores physics concepts, question templates, example problems, and quiz items. A simple relational or document-based structure can support this data.

Key tables or collections include:
- Topics: physics subject areas such as mechanics, energy, and waves.
- Concepts: definitions and explanations for each topic.
- Formulas: mathematical relationships and usage notes.
- Examples: solved problems and step-by-step reasoning.
- Quiz items: multiple-choice or short-answer questions with feedback.

This design enables the chatbot to retrieve content efficiently and update the knowledge base as needed.

#### 3.3.4 Platform Integration (e.g., Telegram/Web)

Platform integration allows learners to access the chatbot through familiar environments. A web interface can provide a simple chat window, while a Telegram bot can deliver the system through messaging.

The integration includes:
- Web front-end for typing questions and viewing responses.
- API endpoints for sending and receiving messages.
- Authentication or session handling for tracking learner progress.

For example, the chatbot can run on a web server and expose a REST API that the user interface calls. In a Telegram deployment, the bot receives messages via webhooks and forwards them to the NLP engine.

#### 3.3.5 Additional Features (Quiz, Image input, etc.)

The chatbot includes additional learning features to enhance student engagement:
- Quiz mode: the bot asks physics questions and evaluates student answers.
- Example problems: the system provides worked examples and explanations.
- Image input support: users can send images of physics diagrams or equations for analysis.
- Progress tracking: the chatbot records completed quizzes and topics reviewed.

These features make the chatbot more interactive and better suited for study practice.

## CHAPTER 4: RESULTS AND EVALUATION

### 4.1 System Performance

The performance of the Physics AI chatbot is evaluated based on response accuracy and user feedback. Testing includes a set of sample questions and a group of learners.

#### 4.1.1 Accuracy of Responses

Response accuracy is measured by comparing chatbot answers to expected explanations. A sample evaluation set of physics questions can include:
- "What is Newton's second law?"
- "How do you calculate gravitational potential energy?"
- "What is the difference between speed and velocity?"

The chatbot is considered accurate if it returns correct, relevant, and clear responses. In the prototype stage, accuracy may range from 75% to 90% depending on the complexity of queries.

#### 4.1.2 User Feedback and Testing

User testing involves having students interact with the chatbot and then providing feedback on usability and helpfulness. Feedback metrics include:
- Ease of use
- Clarity of explanations
- Response speed
- Overall satisfaction

Example feedback comments may include:
- "The chatbot helped me understand kinetic energy more clearly."
- "I liked the example problems and the step-by-step answers."

This qualitative data helps refine the system and identify areas for improvement.

### 4.2 System Usability

System usability is assessed through both user observation and questionnaire responses. A usable chatbot should have a simple interface, fast response time, and helpful guidance.

Key usability findings may include:
- Users appreciate concise explanations with examples.
- Navigation through topics can be improved with menu options.
- Students prefer a conversational tone that is still formal enough for academic content.

If available, metrics such as average session length and number of follow-up questions can indicate how engaged students are with the system.

### 4.3 Evaluation of Learning Improvement

To evaluate learning improvement, the study compares student understanding before and after using the chatbot. Pre-test and post-test questions help measure gains in concept knowledge.

A typical evaluation might show that students who used the chatbot improved their scores on physics concept questions by a measurable margin. Improved confidence in solving problems and clearer conceptual understanding are also valuable outcomes.

For example, a student may move from confusion about projectile motion to correctly identifying the role of horizontal and vertical components in motion analysis.

## CHAPTER 5: DISCUSSION

### 5.1 Conclusion

The Physics AI chatbot developed in this project demonstrates the potential of conversational AI to support student learning. The system provides accessible explanations, physics knowledge retrieval, and interactive quizzes. It addresses common challenges in physics education by offering immediate feedback and reinforcement.

Overall, the study shows that an AI chatbot can be a useful supplementary tool for physics learners. It also illustrates how IT and AI methods can be applied to educational problems in a practical final year project.

### 5.2 Future Work

Future work can extend the system in several directions:
- Expanding the knowledge base to cover more advanced physics topics.
- Incorporating multimedia explanations with diagrams and animations.
- Enhancing NLP capability to handle more complex question structures.
- Enabling adaptive learning paths based on student performance.
- Piloting the chatbot in a larger classroom environment.

These improvements would increase the chatbot’s usefulness and scalability as a comprehensive physics learning assistant.

## REFERENCES

- Brown, T. B., Mann, B., Ryder, N., et al. (2020). Language Models are Few-Shot Learners. *Advances in Neural Information Processing Systems*, 33, 1877–1901.
- Chen, D., Manning, C. D. (2014). A Fast and Accurate Dependency Parser using Neural Networks. *Proceedings of EMNLP*.
- D'Mello, S., & Graesser, A. (2015). Feeling, Thinking, and Computing with Affect-Aware Learning Technologies. *International Journal of Artificial Intelligence in Education*, 24(4), 427-432.
- Goda, K., Boldyreff, C., & Grinberg, Y. (2019). Chatbots in Education: A Systematic Review. *Journal of Educational Technology Systems*, 48(3), 287-307.
- Jurafsky, D., & Martin, J. H. (2020). *Speech and Language Processing* (3rd ed.). Pearson.
- Luckin, R., Holmes, W., Griffiths, M., & Forcier, L. B. (2016). *Intelligence Unleashed: An Argument for AI in Education*. Pearson.
- McTighe, J., & Wiggins, G. (2013). *Understanding by Design*. ASCD.
- Woolf, B. P. (2010). *Building Intelligent Interactive Tutors: Student-Centered Strategies for Revolutionizing e-Learning*. Morgan Kaufmann.

## APPENDICES

### Appendix A: System Installation and Setup

1. Install required software:
   - Python 3.10 or higher
   - Web framework such as Flask or Django
   - NLP library such as spaCy or NLTK
   - Database system such as SQLite or PostgreSQL

2. Set up the project environment:
   - Create a virtual environment: `python -m venv venv`
   - Activate the environment: `venv\Scripts\activate`
   - Install dependencies: `pip install -r requirements.txt`

3. Configure the chatbot:
   - Prepare the physics knowledge base in JSON or database tables.
   - Configure API keys or chatbot tokens for Telegram or web integration.
   - Run the server: `python app.py` or `flask run`.

4. Access the chatbot:
   - Open the web interface in a browser or connect via Telegram.
   - Test with sample questions and confirm responses.

### Appendix B: Chatbot Configuration / Code Snippets

Example chatbot configuration file (JSON):

```
{
  "bot_name": "PhysicsTutorAI",
  "language": "en",
  "default_topic": "mechanics",
  "quiz_enabled": true,
  "knowledge_base": "physics_content.json"
}
```

Example code snippet for question processing:

```python
from nlp_module import parse_question
from knowledge_base import find_answer

user_input = "What is kinetic energy?"
intent, entities = parse_question(user_input)
answer = find_answer(intent, entities)
print(answer)
```

Example sample conversation:

User: "What is Newton's first law?"
Bot: "Newton's first law states that an object at rest remains at rest and an object in motion continues in motion unless acted on by an external force. This is also known as the law of inertia."

User: "Can you give me a simple example?"
Bot: "Yes. A book on a table will remain stationary until you apply a force by pushing it. That demonstrates inertia."

Example quiz interaction:

Bot: "What is the formula for kinetic energy?"
User: "KE = 1/2 mv^2"
Bot: "Correct. Kinetic energy equals one half the mass times the square of the velocity."
