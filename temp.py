import xlrd
import sys
import re


def get_testmodes(target_blk):
    xlsx_file_name = "GPIO_DFTMUX_TOPPIPE.xlsx"
    workbook = xlrd.open_workbook(xlsx_file_name)
    worksheet = workbook.sheet_by_index(0)

    columns = worksheet.row_values(1)
    testmode_indices = [idx for idx in columns if columns[idx] == "Signal"]
    blk_indices = [idx for idx in columns if columns[idx] == "BLK"]

    testmodes = set()

    for idx in range(len(blk_indices)):
        if target_blk in worksheet.col_values(blk_indices[idx]):
            testmodes.add(worksheet.row_values(0)[testmode_indices[idx]])

    return testmodes


# case 1)  ~TEST_MODE_MPHY/1'b1
# case 2)  ~(TEST_MODE_MPHY|TEST_MODE_MPHY_SCAN)/1'b1
def change_DESC(desc, testmodes):
    case_1_style = re.compile("~\w+/.+")
    case_2_style = re.compile("~\((\w+\|)+\w+\)/.+")

    if case_1_style.match(desc):
        testmodes_in_desc = set([desc.split('/')[0].replace('~','')])
    elif case_2_style.match(desc):
        testmodes_in_desc = set(desc.split('/')[0].replace('~','').replace('(','').replace(')','').split('|'))
    else:
        print("invalid DESC format : " + desc)
        exit()

    target_testmodes = testmodes - testmodes_in_desc
    target_testmodes = list(target_testmodes)
    target_testmodes.sort()

    new_desc = ""
    for testmode in target_testmodes:
        new_desc = new_desc + testmode + '/' + desc.split('/')[1] + '/'

    return new_desc[0:-1]


desc = sys.argv[1]
blk = sys.argv[2]

if '~' in desc:
    target_index = desc.split('~')[0].count('/')
    before = desc.split('/')[target_index] + '/' + desc.split('/')[target_index + 1]
    after = change_DESC(before, get_testmodes(blk))
    desc = desc.replace(before, after)
    print(desc)