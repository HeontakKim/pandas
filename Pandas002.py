import pandas
import mymodule

# 2강. Dataframe을 file에서 읽어오는 여러가지 방법

# 1) 가장 기본. csv 파일에서 읽어오는 방법
dataframe = pandas.read_csv("temp.csv")
mymodule.printline(dataframe)

# 2) txt 파일도 read_csv 내장함수로 읽어올 수 있다.
# 만약, data를 구분하는 수단이 쉼표가 아니라면, delimiter에 추가해야 한다.
# 만약, file에서 첫 줄이 각 column name이 아니고 실 데이터부터 시작한다면, header에 None이라고 알려줘야 한다.
dataframe = pandas.read_csv("temp.txt", delimiter='.', header=None)
mymodule.printline(dataframe)

# column name이 없을 때, 이를 넣어주고 싶다면 column을 list로 추가해줄 수 있다.
dataframe.columns = ["Name", "Age", "Job"]
mymodule.printline(dataframe)

# read_csv할 때 그냥 one-step으로 column name을 지어줄 수도 있다.
dataframe = pandas.read_csv("temp.txt", delimiter='.', header=None, names=["NAME", "AGE", "JOB"])
mymodule.printline(dataframe)