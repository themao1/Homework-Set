def DateQuery(TableName = 'banks',QueryDate = 'name'):
    '''
    表信息查询
    :param TableName:
    :param QueryDate:
    :return: list
    '''
    cursor.execute("select * from " + TableName + " where " + QueryDate)

def DateInsert(insertlist) -> None:
    '''
    插入数据
    :param insertlist:
    :return: None
    '''
